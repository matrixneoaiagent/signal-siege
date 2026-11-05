#!/usr/bin/env python3
"""
Signal Siege — a tiny turn-based strategy game for humans and agents.

Standard library only. The whole game lives in memory; restarting the server
resets every match. Run with:

    python3 game_server.py
    # http://127.0.0.1:3100

HTTP API and browser UI are served by the same process. See README.md.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Rules constants
# ---------------------------------------------------------------------------

BOARD_SIZE = 5
MIN_PLAYERS = 2
MAX_PLAYERS = 4
# Four side-midpoint nodes; control any three to win. Four nodes keep the map
# symmetric for every corner start and avoid a single central chokepoint.
NODE_CELLS: tuple[tuple[int, int], ...] = ((0, 2), (2, 0), (2, 4), (4, 2))
NODE_BONUS = 3
NODE_TARGET = 3
MATCH_SECONDS = 8 * 60
TURN_SECONDS = 30
MAX_MATCHES = 500
MAX_BODY_BYTES = 64 * 1024

COLORS = ("#ff6b6b", "#4dabf7", "#51cf66", "#ffd43b")
BOT_NAMES = ("Rook", "Vega", "Nyx")

# Start cells by seat count. Every corner has the same distance profile to the
# three nodes, so the map is symmetric for two and four players.
START_POSITIONS: dict[int, tuple[tuple[int, int], ...]] = {
    2: ((0, 0), (4, 4)),
    3: ((0, 0), (0, 4), (4, 4)),
    4: ((0, 0), (0, 4), (4, 4), (4, 0)),
}

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COLS = "ABCDE"

Json = dict[str, Any]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


class GameError(Exception):
    """An error that is safe and useful to show to an API client."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def clean_name(raw: Any) -> str:
    if not isinstance(raw, str):
        raise GameError(400, "invalid_name", "Name must be a string.")
    name = " ".join(raw.split())
    if not name:
        raise GameError(400, "invalid_name", "Name is required.")
    if len(name) > 24:
        raise GameError(400, "invalid_name", "Name must be 24 characters or fewer.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise GameError(400, "invalid_name", "Name cannot contain control characters.")
    return name


def parse_bounded_int(value: Any, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise GameError(400, f"invalid_{field}", f"{field} must be a number.")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise GameError(400, f"invalid_{field}", f"{field} must be a number.")
    if not low <= value <= high:
        raise GameError(
            400, f"invalid_{field}", f"{field} must be between {low} and {high}."
        )
    return value


def parse_player_id(value: Any) -> int:
    if isinstance(value, bool):
        raise GameError(400, "invalid_player", "player must be a number.")
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    if isinstance(value, int):
        return value
    raise GameError(400, "invalid_player", "player must be a number.")


def parse_target(raw: Any) -> tuple[int, int]:
    if (
        not isinstance(raw, list)
        or len(raw) != 2
        or any(isinstance(v, bool) or not isinstance(v, int) for v in raw)
    ):
        raise GameError(
            400, "invalid_target", "target must be [row, col] with integer values."
        )
    row, col = raw
    if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
        raise GameError(
            400,
            "invalid_target",
            f"target must be inside the {BOARD_SIZE}x{BOARD_SIZE} board.",
        )
    return row, col


def neighbors(row: int, col: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        r, c = row + dr, col + dc
        if 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE:
            out.append((r, c))
    return out


def coord_label(row: int, col: int) -> str:
    return f"{COLS[col]}{row + 1}"


# ---------------------------------------------------------------------------
# Game model
# ---------------------------------------------------------------------------


class Cell:
    __slots__ = ("owner", "strength", "node")

    def __init__(self, node: bool = False) -> None:
        self.owner: int | None = None
        self.strength = 0
        self.node = node

    def as_dict(self) -> Json:
        return {"owner": self.owner, "strength": self.strength, "node": self.node}


@dataclass
class Player:
    id: int
    name: str
    token: str
    color: str
    bot: bool = False


class Match:
    """One in-memory match. Public methods are used while holding GameStore.lock."""

    def __init__(self, code: str, seats: int, created_at: float) -> None:
        self.code = code
        self.seats = seats
        self.players: dict[int, Player] = {}
        self.board: list[list[Cell]] = [
            [Cell((r, c) in NODE_CELLS) for c in range(BOARD_SIZE)]
            for r in range(BOARD_SIZE)
        ]
        self.status = "lobby"  # lobby | active | finished
        self.created_at = created_at
        self.started_at: float | None = None
        self.deadline: float | None = None
        self.finished_at: float | None = None
        self.current: int | None = None
        self.turn_number = 0
        self.turn_started_at: float | None = None
        self.move_log: list[Json] = []
        self.initial_board = self.snapshot_board()
        self.winner: int | None = None
        self.finish_code: str | None = None
        self.finish_reason: str | None = None
        self.bot_rng = random.Random(secrets.randbits(64))

    # -- players ---------------------------------------------------------

    def _name_taken(self, name: str) -> bool:
        folded = name.casefold()
        return any(p.name.casefold() == folded for p in self.players.values())

    def add_player(self, name: str, bot: bool = False) -> Player:
        if self.status != "lobby":
            raise GameError(409, "match_not_joinable", "This match is not in the lobby.")
        free = [i for i in range(self.seats) if i not in self.players]
        if not free:
            raise GameError(409, "match_full", "This match is full.")
        if self._name_taken(name):
            raise GameError(
                409, "name_taken", "That name is already taken in this match."
            )
        seat = free[0]
        player = Player(
            id=seat,
            name=name,
            token=secrets.token_hex(16),
            color=COLORS[seat],
            bot=bot,
        )
        self.players[seat] = player
        return player

    @property
    def host_id(self) -> int | None:
        return min(self.players) if self.players else None

    def _next_bot_name(self) -> str:
        for base in BOT_NAMES:
            if not self._name_taken(base):
                return base
        for suffix in range(2, 20):
            for base in BOT_NAMES:
                candidate = f"{base} {suffix}"
                if not self._name_taken(candidate):
                    return candidate
        raise GameError(503, "bot_name_unavailable", "Could not name another bot.")

    def add_bot(self) -> Player:
        return self.add_player(self._next_bot_name(), bot=True)

    # -- lifecycle -------------------------------------------------------

    def start(self, now: float) -> None:
        if self.status != "lobby":
            raise GameError(409, "wrong_status", "This match has already started.")
        if len(self.players) < MIN_PLAYERS:
            raise GameError(
                409, "not_enough_players", "At least two players are needed to start."
            )
        starts = START_POSITIONS[self.seats]
        for pid in self.players:
            r, c = starts[pid]
            self.board[r][c].owner = pid
            self.board[r][c].strength = 2
        self.status = "active"
        self.started_at = now
        self.deadline = now + MATCH_SECONDS
        self.current = min(self.players)
        self.turn_number = 1
        self.turn_started_at = now
        self.initial_board = self.snapshot_board()

    def _finish(self, code: str, now: float, winner: int | None = None) -> None:
        if self.status == "finished":
            return
        ranking = self.ranking()
        if not ranking:
            winner = None
        elif code == "time":
            winner = ranking[0]["player"]
        self.status = "finished"
        self.finished_at = now
        self.current = None
        self.winner = winner
        self.finish_code = code
        if code == "time" and winner is not None:
            top = next(row for row in ranking if row["player"] == winner)
            self.finish_reason = (
                f"Time limit — {top['name']} wins on points ({top['score']})."
            )
        elif code == "nodes" and winner is not None:
            self.finish_reason = (
                f"{self.players[winner].name} controls {NODE_TARGET} of the "
                f"{len(NODE_CELLS)} signal nodes."
            )
        elif code == "last_standing" and winner is not None:
            self.finish_reason = f"{self.players[winner].name} is the last player standing."
        else:
            self.finish_reason = "Match finished."

    def _advance_turn(self, now: float) -> None:
        if self.status != "active":
            return
        order = sorted(self.players)
        alive = [pid for pid in order if self.stats(pid)["cells"] > 0]
        if not alive:
            self._finish("last_standing", now)
            return
        try:
            start = order.index(self.current) if self.current is not None else -1
        except ValueError:
            start = -1
        for step in range(1, len(order) + 1):
            candidate = order[(start + step) % len(order)]
            if candidate in alive:
                self.current = candidate
                break
        self.turn_started_at = now

    def pump(self, now: float, max_actions: int = 200) -> None:
        """Let bots play, and auto-play human turns that hit the 30s clock."""
        if self.status != "active":
            return
        for _ in range(max_actions):
            if self.status != "active":
                return
            if self.deadline is not None and now >= self.deadline:
                self._finish("time", now)
                return
            player = self.players.get(self.current) if self.current is not None else None
            if player is None:
                self._finish("last_standing", now)
                return
            started = self.turn_started_at if self.turn_started_at is not None else now
            if not player.bot and now - started < TURN_SECONDS:
                return
            move = choose_move(self, player.id, self.bot_rng)
            if move is None:
                self._advance_turn(now)
                continue
            self.apply_move(
                player.id,
                move["action"],
                move["target"],
                now,
                automatic=not player.bot,
            )

    # -- scoring ---------------------------------------------------------

    def stats(self, pid: int) -> Json:
        cells = nodes = strength = 0
        for row in self.board:
            for cell in row:
                if cell.owner == pid:
                    cells += 1
                    strength += cell.strength
                    if cell.node:
                        nodes += 1
        return {
            "player": pid,
            "cells": cells,
            "nodes": nodes,
            "strength": strength,
            "score": cells + NODE_BONUS * nodes,
            "alive": cells > 0,
        }

    def ranking(self) -> list[Json]:
        rows: list[Json] = []
        for pid, player in sorted(self.players.items()):
            row = self.stats(pid)
            row.update(
                {"name": player.name, "color": player.color, "bot": player.bot}
            )
            rows.append(row)
        # An instant node win takes precedence over points, so the winner must
        # be #1 even if another player ended up with a higher raw score.
        finished_winner = self.winner if self.status == "finished" else None
        rows.sort(
            key=lambda row: (
                0
                if finished_winner is not None and row["player"] == finished_winner
                else 1,
                -row["score"],
                -row["nodes"],
                -row["strength"],
                row["player"],
            )
        )
        for index, row in enumerate(rows, start=1):
            row["rank"] = index
        return rows

    def snapshot_board(self) -> list[list[Json]]:
        return [[cell.as_dict() for cell in row] for row in self.board]

    # -- moves -----------------------------------------------------------

    def legal_moves(self, pid: int) -> list[Json]:
        if self.status != "active" or pid not in self.players:
            return []
        moves: dict[tuple[str, int, int], Json] = {}
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                source = self.board[r][c]
                if source.owner != pid:
                    continue
                for nr, nc in neighbors(r, c):
                    target = self.board[nr][nc]
                    node = target.node
                    if target.owner is None:
                        moves.setdefault(
                            ("claim", nr, nc),
                            {
                                "action": "claim",
                                "target": [nr, nc],
                                "node": node,
                            },
                        )
                    elif target.owner == pid:
                        if target.strength < 2:
                            moves.setdefault(
                                ("fortify", nr, nc),
                                {
                                    "action": "fortify",
                                    "target": [nr, nc],
                                    "node": node,
                                },
                            )
                    else:
                        key = ("attack", nr, nc)
                        if key in moves:
                            continue
                        power = 1 + sum(
                            1
                            for ar, ac in neighbors(nr, nc)
                            if self.board[ar][ac].owner == pid
                        )
                        defense = target.strength
                        moves[key] = {
                            "action": "attack",
                            "target": [nr, nc],
                            "node": node,
                            "power": power,
                            "defense": defense,
                            "captures": power > defense,
                            "defender": target.owner,
                        }
        order = {"claim": 0, "fortify": 1, "attack": 2}
        return sorted(
            moves.values(),
            key=lambda move: (
                order[move["action"]],
                move["target"][0],
                move["target"][1],
            ),
        )

    def apply_move(
        self,
        pid: int,
        action: str,
        target: Any,
        now: float,
        automatic: bool = False,
    ) -> Json:
        if self.status != "active":
            raise GameError(409, "match_not_active", "This match is not active.")
        player = self.players.get(pid)
        if player is None:
            raise GameError(403, "unknown_player", "That player is not in this match.")
        if self.current != pid:
            raise GameError(403, "not_your_turn", "It is not your turn.")
        if not isinstance(action, str):
            raise GameError(400, "invalid_action", "action must be a string.")
        action = action.strip().lower()
        if action not in ("claim", "fortify", "attack"):
            raise GameError(
                400,
                "invalid_action",
                "action must be claim, fortify, or attack.",
            )
        r, c = parse_target(target)
        cell = self.board[r][c]
        power: int | None = None
        defense: int | None = None

        if action == "claim":
            if cell.owner is not None:
                raise GameError(400, "illegal_move", "That cell is already owned.")
            if not any(self.board[nr][nc].owner == pid for nr, nc in neighbors(r, c)):
                raise GameError(
                    400,
                    "illegal_move",
                    "You can only claim a cell adjacent to your territory.",
                )
            cell.owner = pid
            cell.strength = 1
            result = "claimed"
            detail = (
                f"Seized signal node {coord_label(r, c)}"
                if cell.node
                else f"Claimed {coord_label(r, c)}"
            )
        elif action == "fortify":
            if cell.owner != pid:
                raise GameError(400, "illegal_move", "You can only fortify your own cell.")
            if cell.strength >= 2:
                raise GameError(400, "illegal_move", "That cell is already fortified.")
            cell.strength = 2
            result = "fortified"
            detail = f"Fortified {coord_label(r, c)}"
        else:
            if cell.owner is None:
                raise GameError(400, "illegal_move", "There is no enemy on that cell.")
            if cell.owner == pid:
                raise GameError(400, "illegal_move", "That cell is already yours.")
            if not any(self.board[nr][nc].owner == pid for nr, nc in neighbors(r, c)):
                raise GameError(
                    400,
                    "illegal_move",
                    "You can only attack a cell adjacent to your territory.",
                )
            defense = cell.strength
            power = 1 + sum(
                1
                for nr, nc in neighbors(r, c)
                if self.board[nr][nc].owner == pid
            )
            if power > defense:
                cell.owner = pid
                cell.strength = 1
                result = "captured"
                detail = (
                    f"Attack {power} vs defense {defense} — captured "
                    f"{coord_label(r, c)}"
                )
            else:
                result = "repelled"
                detail = (
                    f"Attack {power} vs defense {defense} — "
                    f"{coord_label(r, c)} held"
                )

        record: Json = {
            "seq": len(self.move_log) + 1,
            "turn": self.turn_number,
            "player": pid,
            "player_name": player.name,
            "action": action,
            "target": [r, c],
            "result": result,
            "detail": detail,
            "at": round(max(0.0, now - (self.started_at or now)), 1),
            "auto": automatic,
            "bot": player.bot,
            "board": self.snapshot_board(),
        }
        if power is not None and defense is not None:
            record["power"] = power
            record["defense"] = defense
        self.move_log.append(record)

        # Win conditions, in priority order.
        if self.stats(pid)["nodes"] >= NODE_TARGET:
            self._finish("nodes", now, winner=pid)
            return record
        alive = [
            other
            for other in self.players
            if self.stats(other)["cells"] > 0
        ]
        if len(alive) <= 1:
            self._finish(
                "last_standing", now, winner=alive[0] if alive else None
            )
            return record
        if self.deadline is not None and now >= self.deadline:
            self._finish("time", now)
            return record

        self.turn_number += 1
        self._advance_turn(now)
        return record

    # -- serialization ---------------------------------------------------

    def to_state(self, now: float, viewer: int | None = None) -> Json:
        ranking = self.ranking()
        by_player = {row["player"]: row for row in ranking}
        players: list[Json] = []
        for pid, player in sorted(self.players.items()):
            row = by_player[pid]
            players.append(
                {
                    "id": pid,
                    "name": player.name,
                    "color": player.color,
                    "bot": player.bot,
                    "alive": row["alive"],
                    "cells": row["cells"],
                    "nodes": row["nodes"],
                    "strength": row["strength"],
                    "score": row["score"],
                    "rank": row["rank"],
                }
            )

        if self.status == "active" and self.deadline is not None:
            match_seconds_left = max(0, math.ceil(self.deadline - now))
        else:
            match_seconds_left = 0
        if (
            self.status == "active"
            and self.turn_started_at is not None
        ):
            turn_seconds_left = max(
                0, math.ceil(TURN_SECONDS - (now - self.turn_started_at))
            )
        else:
            turn_seconds_left = 0

        moves: list[Json] = []
        for record in self.move_log[-50:]:
            moves.append({key: value for key, value in record.items() if key != "board"})

        current_name = (
            self.players[self.current].name
            if self.current is not None and self.current in self.players
            else None
        )
        winner_name = (
            self.players[self.winner].name
            if self.winner is not None and self.winner in self.players
            else None
        )
        host = self.host_id
        legal = (
            self.legal_moves(self.current)
            if self.status == "active" and self.current is not None
            else []
        )
        return {
            "match": self.code,
            "status": self.status,
            "seats": self.seats,
            "joined": len(self.players),
            "host": host,
            "you": viewer,
            "your_turn": viewer is not None and self.status == "active" and self.current == viewer,
            "is_host": viewer is not None and viewer == host,
            "can_add_bot": (
                self.status == "lobby"
                and viewer is not None
                and viewer == host
                and len(self.players) < self.seats
            ),
            "can_start": (
                self.status == "lobby"
                and viewer is not None
                and viewer == host
                and len(self.players) >= MIN_PLAYERS
            ),
            "board": self.snapshot_board(),
            "players": players,
            "ranking": ranking,
            "current": self.current if self.status == "active" else None,
            "current_name": current_name,
            "turn": self.turn_number,
            "move_count": len(self.move_log),
            "moves": moves,
            "last_move": moves[-1] if moves else None,
            "legal": legal,
            "nodes": [list(cell) for cell in NODE_CELLS],
            "node_bonus": NODE_BONUS,
            "node_target": NODE_TARGET,
            "match_seconds": MATCH_SECONDS,
            "match_seconds_left": match_seconds_left,
            "turn_seconds": TURN_SECONDS,
            "turn_seconds_left": turn_seconds_left,
            "started_at": self.started_at,
            "deadline": self.deadline,
            "finished_at": self.finished_at,
            "winner": self.winner,
            "winner_name": winner_name,
            "finish_code": self.finish_code,
            "finish_reason": self.finish_reason,
            "server_time": now,
        }

    def replay_payload(self) -> Json:
        winner_name = (
            self.players[self.winner].name
            if self.winner is not None and self.winner in self.players
            else None
        )
        return {
            "match": self.code,
            "status": self.status,
            "seats": self.seats,
            "players": [
                {
                    "id": player.id,
                    "name": player.name,
                    "color": player.color,
                    "bot": player.bot,
                }
                for _, player in sorted(self.players.items())
            ],
            "initial_board": self.initial_board,
            "frames": self.move_log,
            "frame_count": len(self.move_log),
            "nodes": [list(cell) for cell in NODE_CELLS],
            "node_bonus": NODE_BONUS,
            "winner": self.winner,
            "winner_name": winner_name,
            "finish_code": self.finish_code,
            "finish_reason": self.finish_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# Server-side bot
# ---------------------------------------------------------------------------


def choose_move(
    match: Match, pid: int, rng: random.Random | None = None
) -> Json | None:
    """A small, readable heuristic bot. Returns a legal move or None."""
    rng = rng or random.Random()
    moves = match.legal_moves(pid)
    if not moves:
        return None
    me = match.stats(pid)
    opponents = [match.stats(other) for other in match.players if other != pid]
    opponent_max_nodes = max((stats["nodes"] for stats in opponents), default=0)
    alive_players = sum(
        1 for other in match.players if match.stats(other)["cells"] > 0
    )
    one_node_from_win = me["nodes"] == NODE_TARGET - 1
    board = match.board
    node_set = set(NODE_CELLS)
    my_nodes = {
        (r, c) for r, c in node_set if board[r][c].owner == pid
    }
    missing_nodes = [
        (r, c) for r, c in node_set if board[r][c].owner != pid
    ]
    recent_attack_targets = [
        tuple(record["target"])
        for record in match.move_log[-10:]
        if record.get("action") == "attack"
    ]
    best: Json | None = None
    best_score = float("-inf")

    for move in moves:
        action = move["action"]
        r, c = move["target"]
        cell = board[r][c]
        score = 0.0

        if action == "claim":
            score = 16
            if move["node"]:
                score += 50
            own_neighbors = sum(
                1
                for nr, nc in neighbors(r, c)
                if board[nr][nc].owner == pid
            )
            enemy_neighbors = sum(
                1
                for nr, nc in neighbors(r, c)
                if board[nr][nc].owner not in (None, pid)
            )
            score += own_neighbors * 4
            score += enemy_neighbors * 2
            score += max(0, 3 - (abs(r - 2) + abs(c - 2))) * 2
            if me["cells"] <= 2:
                score += 8
            if move["node"] and opponent_max_nodes == NODE_TARGET - 1:
                score += 90
            if move["node"] and one_node_from_win:
                score += 1000
            if one_node_from_win:
                # Flank the last missing node rather than fighting elsewhere.
                for nr, nc in missing_nodes:
                    if abs(nr - r) + abs(nc - c) == 1:
                        score += 72
            if any(
                abs(nr - r) + abs(nc - c) == 1
                for nr, nc in node_set
                if board[nr][nc].owner not in (None, pid)
            ):
                score += 14

        elif action == "fortify":
            score = 8
            if move["node"]:
                score += 20
            threats = 0
            for nr, nc in neighbors(r, c):
                enemy = board[nr][nc]
                if enemy.owner in (None, pid):
                    continue
                enemy_power = 1 + sum(
                    1
                    for ar, ac in neighbors(r, c)
                    if board[ar][ac].owner == enemy.owner
                )
                if enemy_power > cell.strength:
                    threats += 1
            score += threats * 22
            if move["node"] and threats:
                score += 22
            if one_node_from_win and (r, c) in my_nodes:
                score += 45
            if opponent_max_nodes == NODE_TARGET - 1 and (r, c) in my_nodes:
                score += 45
            if me["cells"] == 1:
                score += 25

        else:  # attack
            if move["captures"]:
                score = 22
                if move["node"]:
                    score += 50
                score += move["defense"] * 3
                defender = move.get("defender")
                enemy_support = 0
                if defender is not None:
                    defender_stats = match.stats(defender)
                    if defender_stats["cells"] == 1:
                        score += 35 if alive_players > 2 else 1000
                    enemy_support = sum(
                        1
                        for nr, nc in neighbors(r, c)
                        if board[nr][nc].owner == defender
                    )
                if move["node"] and opponent_max_nodes == NODE_TARGET - 1:
                    score += 150
                if move["node"] and one_node_from_win:
                    score += 1000

                # A capture that the enemy can immediately undo is usually a
                # wasted turn. Prefer expanding or fortifying instead, unless
                # this move wins or denies an immediate node win.
                critical_node_fight = move["node"] and (
                    one_node_from_win or opponent_max_nodes == NODE_TARGET - 1
                )
                if not critical_node_fight and enemy_support:
                    score -= 18 + enemy_support * 8
                repeats = recent_attack_targets.count((r, c))
                if not critical_node_fight:
                    score -= repeats * 16
                if move["node"] and not critical_node_fight:
                    score -= 8
            else:
                score = -45

        score += rng.uniform(-2.5, 2.5)
        if score > best_score:
            best_score = score
            best = move
    return best


# ---------------------------------------------------------------------------
# Store: concurrency + lifecycle
# ---------------------------------------------------------------------------


class GameStore:
    """Thread-safe in-memory match store."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.RLock()
        self._matches: dict[str, Match] = {}
        self._clock = clock

    # -- lookup ----------------------------------------------------------

    def _find(self, code: Any) -> Match:
        if not isinstance(code, str) or not code.strip():
            raise GameError(400, "missing_match", "match code is required.")
        normalized = code.strip().upper()
        match = self._matches.get(normalized)
        if match is None:
            raise GameError(
                404,
                "match_not_found",
                "No match with that code. Check the code and try again.",
            )
        return match

    def _auth(self, match: Match, pid_value: Any, token: Any) -> Player:
        pid = parse_player_id(pid_value)
        player = match.players.get(pid)
        if player is None:
            raise GameError(403, "unknown_player", "That player is not in this match.")
        if not isinstance(token, str) or not secrets.compare_digest(
            player.token, token
        ):
            raise GameError(401, "bad_token", "Invalid player token.")
        return player

    def _new_code(self) -> str:
        for _ in range(100):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
            if code not in self._matches:
                return code
        raise GameError(503, "could_not_allocate", "Could not allocate a match code.")

    def _cleanup(self, now: float) -> None:
        if len(self._matches) < MAX_MATCHES:
            return
        for code, match in list(self._matches.items()):
            if (
                match.status == "finished"
                and match.finished_at is not None
                and now - match.finished_at > 30 * 60
            ):
                del self._matches[code]
        if len(self._matches) >= MAX_MATCHES:
            raise GameError(
                503,
                "server_full",
                "The server is holding too many matches. Try again shortly.",
            )

    # -- public API ------------------------------------------------------

    def create_match(self, name: Any, seats: Any = MIN_PLAYERS) -> tuple[Match, int, str]:
        clean = clean_name(name)
        seat_count = parse_bounded_int(seats, "seats", MIN_PLAYERS, MAX_PLAYERS)
        with self._lock:
            now = self._clock()
            self._cleanup(now)
            code = self._new_code()
            match = Match(code, seat_count, now)
            player = match.add_player(clean)
            self._matches[code] = match
            return match, player.id, player.token

    def join_match(self, code: Any, name: Any) -> tuple[Match, int, str]:
        clean = clean_name(name)
        with self._lock:
            match = self._find(code)
            player = match.add_player(clean)
            return match, player.id, player.token

    def start_match(
        self, code: Any, pid_value: Any, token: Any
    ) -> Json:
        with self._lock:
            match = self._find(code)
            player = self._auth(match, pid_value, token)
            if player.id != match.host_id:
                raise GameError(
                    403, "not_host", "Only the player who created the match can start it."
                )
            now = self._clock()
            match.start(now)
            match.pump(now)
            return match.to_state(now, player.id)

    def add_bots(
        self, code: Any, pid_value: Any, token: Any, count: Any = 1
    ) -> Json:
        with self._lock:
            match = self._find(code)
            player = self._auth(match, pid_value, token)
            if player.id != match.host_id:
                raise GameError(
                    403,
                    "not_host",
                    "Only the player who created the match can add bots.",
                )
            if match.status != "lobby":
                raise GameError(409, "wrong_status", "Bots can only join the lobby.")
            amount = parse_bounded_int(count, "count", 1, MAX_PLAYERS)
            free = match.seats - len(match.players)
            if free <= 0:
                raise GameError(409, "match_full", "This match is already full.")
            for _ in range(min(amount, free)):
                match.add_bot()
            now = self._clock()
            return match.to_state(now, player.id)

    def get_state(self, code: Any, viewer: Any = None) -> Json:
        with self._lock:
            match = self._find(code)
            viewer_id: int | None = None
            if viewer is not None and viewer != "":
                viewer_id = parse_player_id(viewer)
                if viewer_id not in match.players:
                    raise GameError(
                        404, "unknown_player", "That player is not in this match."
                    )
            now = self._clock()
            match.pump(now)
            return match.to_state(now, viewer_id)

    def move(
        self,
        code: Any,
        pid_value: Any,
        token: Any,
        action: Any,
        target: Any,
    ) -> Json:
        with self._lock:
            match = self._find(code)
            player = self._auth(match, pid_value, token)
            if match.status != "active":
                raise GameError(409, "match_not_active", "This match is not active.")
            now = self._clock()
            if match.deadline is not None and now >= match.deadline:
                match._finish("time", now)
                raise GameError(409, "match_over", "The time limit has ended this match.")
            match.apply_move(player.id, action, target, now)
            match.pump(now)
            return match.to_state(now, player.id)

    def replay(self, code: Any) -> Json:
        with self._lock:
            match = self._find(code)
            now = self._clock()
            match.pump(now)
            return match.replay_payload()

    def pump_all(self) -> None:
        with self._lock:
            now = self._clock()
            for match in list(self._matches.values()):
                if match.status != "active":
                    continue
                try:
                    match.pump(now)
                except Exception:
                    traceback.print_exc()

    def stats(self) -> Json:
        with self._lock:
            counts = {"lobby": 0, "active": 0, "finished": 0}
            for match in self._matches.values():
                counts[match.status] = counts.get(match.status, 0) + 1
            return {
                "matches": len(self._matches),
                "lobby": counts["lobby"],
                "active": counts["active"],
                "finished": counts["finished"],
            }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"


class GameHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        store: GameStore,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.store = store


class GameHandler(BaseHTTPRequestHandler):
    server_version = "SignalSiege/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------

    def _common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'self'",
        )

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send(status, body)

    def _empty(self, status: int = HTTPStatus.NO_CONTENT) -> None:
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> Json:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise GameError(400, "invalid_request", "Invalid Content-Length.")
        if length < 0:
            raise GameError(400, "invalid_request", "Invalid Content-Length.")
        if length > MAX_BODY_BYTES:
            raise GameError(413, "payload_too_large", "Request body is too large.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GameError(400, "invalid_json", "Request body must be valid JSON.")
        if not isinstance(data, dict):
            raise GameError(400, "invalid_json", "Request body must be a JSON object.")
        return data

    def _serve_index(self) -> None:
        try:
            body = INDEX_FILE.read_bytes()
        except OSError:
            self._json(
                500,
                {
                    "error": "The browser UI file is missing.",
                    "code": "ui_missing",
                },
            )
            return
        self._send(200, body, "text/html; charset=utf-8")

    def _query_arg(
        self, query: dict[str, list[str]], key: str, required: bool = False
    ) -> str | None:
        values = query.get(key)
        if not values:
            if required:
                raise GameError(400, "missing_field", f"{key} is required.")
            return None
        return values[0]

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, GameError):
            self._json(exc.status, {"error": exc.message, "code": exc.code})
            return
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        traceback.print_exc()
        try:
            self._json(
                500,
                {
                    "error": "Internal server error.",
                    "code": "internal_error",
                },
            )
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- HTTP methods ----------------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._common_headers()
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)
            if path in ("/", "/index.html"):
                self._serve_index()
                return
            if path == "/favicon.ico":
                self._empty(HTTPStatus.NO_CONTENT)
                return
            if path == "/api/health":
                payload = {"ok": True, "game": "signal-siege"}
                payload.update(self.server.store.stats())
                self._json(200, payload)
                return
            if path == "/api/state":
                code = self._query_arg(query, "match", required=True)
                viewer = self._query_arg(query, "player")
                self._json(200, self.server.store.get_state(code, viewer))
                return
            if path == "/api/replay":
                code = self._query_arg(query, "match", required=True)
                self._json(200, self.server.store.replay(code))
                return
            self._json(
                404,
                {"error": "Not found.", "code": "not_found"},
            )
        except Exception as exc:  # noqa: BLE001 - turned into a JSON error
            self._handle_error(exc)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            data = self._read_json()
            store: GameStore = self.server.store
            if path == "/api/create":
                match, pid, token = store.create_match(
                    data.get("name"), data.get("seats", MIN_PLAYERS)
                )
                self._json(
                    201,
                    {
                        "match": match.code,
                        "player": pid,
                        "token": token,
                        "seats": match.seats,
                    },
                )
                return
            if path == "/api/join":
                match, pid, token = store.join_match(
                    data.get("match"), data.get("name")
                )
                self._json(
                    201,
                    {
                        "match": match.code,
                        "player": pid,
                        "token": token,
                        "seats": match.seats,
                    },
                )
                return
            if path == "/api/start":
                state = store.start_match(
                    data.get("match"), data.get("player"), data.get("token")
                )
                self._json(200, state)
                return
            if path == "/api/bot":
                state = store.add_bots(
                    data.get("match"),
                    data.get("player"),
                    data.get("token"),
                    data.get("count", 1),
                )
                self._json(200, state)
                return
            if path == "/api/move":
                state = store.move(
                    data.get("match"),
                    data.get("player"),
                    data.get("token"),
                    data.get("action"),
                    data.get("target"),
                )
                self._json(200, state)
                return
            self._json(
                404,
                {"error": "Not found.", "code": "not_found"},
            )
        except Exception as exc:  # noqa: BLE001 - turned into a JSON error
            self._handle_error(exc)

    def _method_not_allowed(self) -> None:
        self._json(
            405,
            {"error": "Method not allowed.", "code": "method_not_allowed"},
        )

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Keep polling quiet; surface errors only.
        try:
            numeric = int(code)
        except (TypeError, ValueError):
            numeric = 0
        if numeric >= 400:
            super().log_request(code, size)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Signal Siege game server.")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 to expose)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "3100")),
        help="Port (default: 3100)",
    )
    parser.add_argument(
        "--no-ticker",
        action="store_true",
        help="Disable the background clock (useful for debugging)",
    )
    args = parser.parse_args(argv)

    if not INDEX_FILE.exists():
        print(f"warning: {INDEX_FILE} is missing; the browser UI will not load.", file=sys.stderr)

    store = GameStore()
    try:
        server = GameHTTPServer((args.host, args.port), GameHandler, store)
    except OSError as exc:
        print(
            f"Could not bind {args.host}:{args.port}: {exc}. "
            "Is another server already running?",
            file=sys.stderr,
        )
        return 1

    stop_event = threading.Event()

    if not args.no_ticker:

        def ticker() -> None:
            while not stop_event.wait(1.0):
                store.pump_all()

        thread = threading.Thread(target=ticker, name="signal-siege-clock", daemon=True)
        thread.start()

    print(f"Signal Siege is running at http://{args.host}:{args.port}", flush=True)
    print("Ctrl+C to stop. Matches are in memory and reset on restart.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Signal Siege.", flush=True)
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
