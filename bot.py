#!/usr/bin/env python3
"""
Signal Siege — sample HTTP/JSON bot.

This is a standalone agent client: it joins (or creates) a match over the public
API, reads state, and submits a move on its turn. It uses only the standard
library and does not import the server.

Examples
--------
# Join a match by code. The host starts it when everyone is ready.
python3 bot.py --match ABC123 --name Scout

# Create a match and wait for a human (or another bot) to join. The bot is the
# host, so it starts the match automatically once a second player arrives.
python3 bot.py --create --name Scout --seats 2

# Point at another server:
python3 bot.py --base-url http://127.0.0.1:3100 --match ABC123 --name Scout
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

COLS = "ABCDE"
NODE_TARGET = 3


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = self.base_url + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {}
            raise ApiError(
                exc.code,
                parsed.get("code", "http_error"),
                parsed.get("error", f"HTTP {exc.code}"),
            ) from exc
        except urllib.error.URLError as exc:
            raise ApiError(
                0,
                "connection_error",
                f"Could not reach {self.base_url}: {exc.reason}",
            ) from exc
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ApiError(0, "bad_response", "Server returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ApiError(0, "bad_response", "Server returned a non-object response.")
        return parsed

    def create(self, name: str, seats: int) -> dict:
        return self.request("POST", "/api/create", {"name": name, "seats": seats})

    def join(self, match: str, name: str) -> dict:
        return self.request("POST", "/api/join", {"match": match, "name": name})

    def start(self, match: str, player: int, token: str) -> dict:
        return self.request(
            "POST",
            "/api/start",
            {"match": match, "player": player, "token": token},
        )

    def state(self, match: str, player: int) -> dict:
        return self.request(
            "GET", f"/api/state?match={match}&player={player}"
        )

    def move(
        self, match: str, player: int, token: str, action: str, target: list[int]
    ) -> dict:
        return self.request(
            "POST",
            "/api/move",
            {
                "match": match,
                "player": player,
                "token": token,
                "action": action,
                "target": target,
            },
        )


def coord(row: int, col: int) -> str:
    return f"{COLS[col]}{row + 1}"


def neighbors(row: int, col: int) -> list[tuple[int, int]]:
    result = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        r, c = row + dr, col + dc
        if 0 <= r < 5 and 0 <= c < 5:
            result.append((r, c))
    return result


def choose_move(state: dict, rng: random.Random) -> dict | None:
    """A deliberately basic policy: value nodes, captures, and safe expansion."""
    moves = state.get("legal") or []
    if not moves:
        return None

    me = next(
        (player for player in state["players"] if player["id"] == state["you"]),
        None,
    )
    if me is None:
        return None
    my_nodes = me["nodes"]
    opponents = [p for p in state["players"] if p["id"] != me["id"]]
    opponent_max_nodes = max((p["nodes"] for p in opponents), default=0)
    alive = sum(1 for p in state["players"] if p["alive"])
    board = state["board"]
    one_from_win = my_nodes == NODE_TARGET - 1
    node_cells = [
        (row, col)
        for row in range(5)
        for col in range(5)
        if board[row][col]["node"]
    ]
    missing_nodes = [
        (row, col)
        for row, col in node_cells
        if board[row][col]["owner"] != me["id"]
    ]
    recent_attack_targets = [
        tuple(move["target"])
        for move in (state.get("moves") or [])[-10:]
        if move.get("action") == "attack"
    ]
    best = None
    best_score = float("-inf")

    for move in moves:
        action = move["action"]
        row, col = move["target"]
        cell = board[row][col]
        score = 0.0

        if action == "claim":
            score = 16.0
            if move["node"]:
                score += 50
            own_neighbors = sum(
                1
                for nr, nc in neighbors(row, col)
                if board[nr][nc]["owner"] == me["id"]
            )
            enemy_neighbors = sum(
                1
                for nr, nc in neighbors(row, col)
                if board[nr][nc]["owner"] not in (None, me["id"])
            )
            score += own_neighbors * 4
            score += enemy_neighbors * 2
            if move["node"] and one_from_win:
                score += 1000
            if move["node"] and opponent_max_nodes == NODE_TARGET - 1:
                score += 90
            if one_from_win:
                for nr, nc in missing_nodes:
                    if abs(nr - row) + abs(nc - col) == 1:
                        score += 72
            if any(
                abs(nr - row) + abs(nc - col) == 1
                for nr, nc in node_cells
                if board[nr][nc]["owner"] not in (None, me["id"])
            ):
                score += 14

        elif action == "fortify":
            score = 8.0
            if move["node"]:
                score += 20
            if one_from_win and move["node"]:
                score += 45
            if opponent_max_nodes == NODE_TARGET - 1 and move["node"]:
                score += 45
            threats = 0
            for nr, nc in neighbors(row, col):
                enemy = board[nr][nc]
                if enemy["owner"] is None or enemy["owner"] == me["id"]:
                    continue
                enemy_power = 1 + sum(
                    1
                    for ar, ac in neighbors(row, col)
                    if board[ar][ac]["owner"] == enemy["owner"]
                )
                if enemy_power > cell["strength"]:
                    threats += 1
            score += threats * 22
            if me["cells"] == 1:
                score += 25

        else:  # attack
            if move.get("captures"):
                score = 22.0
                if move["node"]:
                    score += 50
                score += move.get("defense", 1) * 3
                if move["node"] and one_from_win:
                    score += 1000
                if move["node"] and opponent_max_nodes == NODE_TARGET - 1:
                    score += 150
                defender = next(
                    (p for p in state["players"] if p["id"] == move.get("defender")),
                    None,
                )
                enemy_support = sum(
                    1
                    for nr, nc in neighbors(row, col)
                    if board[nr][nc]["owner"] == move.get("defender")
                )
                if defender and defender["cells"] == 1:
                    score += 1000 if alive <= 2 else 40
                critical_node_fight = move["node"] and (
                    one_from_win or opponent_max_nodes == NODE_TARGET - 1
                )
                if not critical_node_fight and enemy_support:
                    score -= 18 + enemy_support * 8
                if not critical_node_fight:
                    score -= recent_attack_targets.count((row, col)) * 16
            else:
                score = -50.0

        score += rng.uniform(-2.5, 2.5)
        if score > best_score:
            best_score = score
            best = move
    return best


def describe_move(move: dict) -> str:
    row, col = move["target"]
    label = coord(row, col)
    if move["action"] == "attack":
        verdict = "captures" if move.get("captures") else "may be repelled"
        return f"attack {label} ({move.get('power')}v{move.get('defense')}, {verdict})"
    if move["action"] == "fortify":
        return f"fortify {label}"
    return f"claim {label}" + (" (signal node)" if move.get("node") else "")


def run(args: argparse.Namespace) -> int:
    client = Client(args.base_url)
    rng = random.Random()

    try:
        if args.create:
            created = client.create(args.name, args.seats)
            match = created["match"]
            player = created["player"]
            token = created["token"]
            print(f"Created match {match}. Share the code; this bot is the host.")
            print("Waiting for a second player. Press Ctrl+C to stop.")
        else:
            joined = client.join(args.match, args.name)
            match = joined["match"]
            player = joined["player"]
            token = joined["token"]
            print(f"Joined match {match} as seat {player} ({args.name}).")

        last_note = None
        while True:
            state = client.state(match, player)

            if state["status"] == "lobby":
                count = len(state["players"])
                if state["is_host"] and count >= 2:
                    print("Second player arrived — starting the match.")
                    state = client.start(match, player, token)
                else:
                    note = f"Lobby: {count}/{state['seats']} seats filled. Waiting for players…"
                    if note != last_note:
                        print(note)
                        last_note = note
                    time.sleep(1.2)
                    continue

            if state["status"] == "finished":
                winner = state.get("winner_name") or "nobody"
                print(f"Match over. Winner: {winner}. {state.get('finish_reason', '')}")
                for row in state.get("ranking", []):
                    print(
                        f"  #{row['rank']} {row['name']} — {row['score']} pts "
                        f"({row['nodes']} nodes, {row['cells']} cells)"
                    )
                return 0

            if state["your_turn"]:
                move = choose_move(state, rng)
                if move is None:
                    time.sleep(0.7)
                    continue
                print(f"Turn {state['turn']}: {describe_move(move)}")
                try:
                    state = client.move(
                        match, player, token, move["action"], move["target"]
                    )
                except ApiError as exc:
                    if exc.code in ("not_your_turn", "illegal_move", "match_not_active"):
                        print(f"  move skipped: {exc.message}")
                        time.sleep(0.4)
                        continue
                    raise
                last = state.get("last_move")
                if last and last.get("player") == player:
                    print(f"  -> {last.get('detail', last.get('result'))}")
                continue

            time.sleep(0.8)
    except ApiError as exc:
        print(f"API error ({exc.code}): {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nBot stopped.", file=sys.stderr)
        return 130


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Signal Siege sample bot (HTTP/JSON client)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--match", help="match code to join")
    group.add_argument(
        "--create", action="store_true", help="create a new match and wait for an opponent"
    )
    parser.add_argument("--name", default="Scout", help="bot display name")
    parser.add_argument(
        "--seats",
        type=int,
        default=2,
        choices=range(2, 5),
        help="seats when creating a match (2-4, default 2)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SIGNAL_SIEGE_URL", "http://127.0.0.1:3100"),
        help="server URL (default: http://127.0.0.1:3100)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Keep status lines visible when stdout is redirected to a log file.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
