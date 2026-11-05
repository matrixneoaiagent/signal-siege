"""Tests for Signal Siege. Run: python3 -m unittest -v test_game.py"""

from __future__ import annotations

import http.client
import json
import random
import threading
import unittest

from game_server import (
    COLORS,
    MATCH_SECONDS,
    NODE_CELLS,
    TURN_SECONDS,
    GameError,
    GameHandler,
    GameHTTPServer,
    GameStore,
    choose_move,
)


class FakeClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_started(
    seats: int = 2, names: tuple[str, ...] | None = None
) -> tuple[GameStore, object, FakeClock, list[tuple[int, str]]]:
    clock = FakeClock()
    store = GameStore(clock=clock)
    names = names or ("Ada", "Bo", "Cy", "Dee")[:seats]
    match, pid, token = store.create_match(names[0], seats)
    players = [(pid, token)]
    for name in names[1:]:
        _, next_pid, next_token = store.join_match(match.code, name)
        players.append((next_pid, next_token))
    store.start_match(match.code, players[0][0], players[0][1])
    return store, match, clock, players


def clear_board(match, placements: list[tuple[int, int, int, int]], current: int = 0) -> None:
    for row in match.board:
        for cell in row:
            cell.owner = None
            cell.strength = 0
    for row, col, pid, strength in placements:
        cell = match.board[row][col]
        cell.owner = pid
        cell.strength = strength
    match.current = current
    match.turn_number = 1
    match.turn_started_at = match.started_at


def error_code(callable_) -> str:
    try:
        callable_()
    except GameError as exc:
        return exc.code
    raise AssertionError("expected GameError")


class TestLobby(unittest.TestCase):
    def test_create_join_start_and_token_privacy(self) -> None:
        clock = FakeClock()
        store = GameStore(clock=clock)
        match, p0, token0 = store.create_match("Ada", 4)
        self.assertEqual(p0, 0)
        self.assertRegex(match.code, r"^[A-HJ-NP-Z2-9]{6}$")
        self.assertEqual(len(token0), 32)
        self.assertNotIn("token", json.dumps(store.get_state(match.code, 0)))

        _, p1, token1 = store.join_match(match.code.lower(), "Bo")
        self.assertEqual(p1, 1)
        self.assertNotEqual(token0, token1)

        state = store.get_state(match.code, 0)
        self.assertEqual(state["status"], "lobby")
        self.assertEqual(state["seats"], 4)
        self.assertEqual(state["joined"], 2)
        self.assertTrue(state["is_host"])
        self.assertTrue(state["can_start"])
        self.assertTrue(state["can_add_bot"])
        self.assertEqual([p["name"] for p in state["players"]], ["Ada", "Bo"])
        self.assertNotIn("token", json.dumps(state))

        with self.assertRaises(GameError) as ctx:
            store.join_match(match.code, "ada")
        self.assertEqual(ctx.exception.code, "name_taken")

        with self.assertRaises(GameError) as ctx:
            store.start_match(match.code, p1, token1)
        self.assertEqual(ctx.exception.code, "not_host")

        started = store.start_match(match.code, p0, token0)
        self.assertEqual(started["status"], "active")
        self.assertEqual(started["current"], 0)
        self.assertEqual(started["turn"], 1)
        self.assertEqual(started["match_seconds_left"], MATCH_SECONDS)
        self.assertEqual(started["node_target"], 3)
        self.assertEqual(started["board"][0][0]["owner"], 0)
        self.assertEqual(started["board"][0][0]["strength"], 2)
        self.assertEqual(started["board"][0][4]["owner"], 1)
        for row, col in NODE_CELLS:
            self.assertTrue(started["board"][row][col]["node"])
        self.assertEqual(
            sum(1 for row in started["board"] for cell in row if cell["node"]), 4
        )
        self.assertTrue(started["your_turn"])
        self.assertFalse(started["can_start"])

        with self.assertRaises(GameError) as ctx:
            store.join_match(match.code, "Eve")
        self.assertEqual(ctx.exception.code, "match_not_joinable")

    def test_match_full(self) -> None:
        store = GameStore(clock=FakeClock())
        match, _, _ = store.create_match("Ada", 2)
        store.join_match(match.code, "Bo")
        with self.assertRaises(GameError) as ctx:
            store.join_match(match.code, "Cy")
        self.assertEqual(ctx.exception.code, "match_full")

    def test_start_requires_two_players(self) -> None:
        store = GameStore(clock=FakeClock())
        match, pid, token = store.create_match("Ada", 2)
        with self.assertRaises(GameError) as ctx:
            store.start_match(match.code, pid, token)
        self.assertEqual(ctx.exception.code, "not_enough_players")

    def test_validation(self) -> None:
        store = GameStore(clock=FakeClock())
        for bad_name in ("", "   ", "x" * 25, None, 7):
            with self.assertRaises(GameError) as ctx:
                store.create_match(bad_name, 2)
            self.assertEqual(ctx.exception.code, "invalid_name")
        for bad_seats in (1, 5, True, "many"):
            with self.assertRaises(GameError) as ctx:
                store.create_match("Ada", bad_seats)
            self.assertEqual(ctx.exception.code, "invalid_seats")
        with self.assertRaises(GameError) as ctx:
            store.join_match("ZZZZZZ", "Bo")
        self.assertEqual(ctx.exception.code, "match_not_found")
        with self.assertRaises(GameError) as ctx:
            store.get_state("ZZZZZZ")
        self.assertEqual(ctx.exception.code, "match_not_found")


class TestTurnsAndMoves(unittest.TestCase):
    def test_claim_turn_order_and_adjacency(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        p1, token1 = players[1]

        state = store.move(match.code, p0, token0, "claim", [0, 1])
        self.assertEqual(state["current"], 1)
        self.assertEqual(state["move_count"], 1)
        self.assertEqual(state["moves"][-1]["detail"], "Claimed B1")
        self.assertEqual(state["players"][0]["cells"], 2)
        self.assertEqual(state["players"][0]["score"], 2)

        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, token0, "claim", [0, 2])
        self.assertEqual(ctx.exception.code, "not_your_turn")

        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p1, token1, "claim", [2, 2])
        self.assertEqual(ctx.exception.code, "illegal_move")

        # The home cell starts fortified, so it cannot be fortified again.
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p1, token1, "fortify", [4, 4])
        self.assertEqual(ctx.exception.code, "illegal_move")

        state = store.move(match.code, p1, token1, "claim", [4, 3])
        self.assertEqual(state["current"], 0)
        state = store.move(match.code, p0, token0, "claim", [0, 2])
        self.assertEqual(state["players"][0]["cells"], 3)
        self.assertEqual(state["players"][0]["nodes"], 1)
        # 3 cells + one +3 signal node.
        self.assertEqual(state["players"][0]["score"], 6)

    def test_auth_and_target_validation(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        p1, token1 = players[1]

        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, "wrong", "claim", [0, 1])
        self.assertEqual(ctx.exception.code, "bad_token")
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, 9, token0, "claim", [0, 1])
        self.assertEqual(ctx.exception.code, "unknown_player")
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, token0, "teleport", [0, 1])
        self.assertEqual(ctx.exception.code, "invalid_action")
        for bad_target in ([0], [0, 1, 2], [-1, 1], [0, 5], ["0", "1"], None):
            with self.assertRaises(GameError) as ctx:
                store.move(match.code, p0, token0, "claim", bad_target)
            self.assertEqual(ctx.exception.code, "invalid_target")
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, token0, "claim", [1, 1])
        self.assertEqual(ctx.exception.code, "illegal_move")
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, token0, "attack", [4, 4])
        self.assertEqual(ctx.exception.code, "illegal_move")

        # Invalid token must not reveal whether a move would be legal.
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p1, token0, "claim", [4, 3])
        self.assertEqual(ctx.exception.code, "bad_token")

    def test_fortify_and_defense(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        p1, token1 = players[1]
        state = store.move(match.code, p0, token0, "claim", [0, 1])
        state = store.move(match.code, p1, token1, "claim", [4, 3])
        state = store.move(match.code, p0, token0, "fortify", [0, 1])
        self.assertEqual(state["board"][0][1]["strength"], 2)
        self.assertEqual(state["moves"][-1]["result"], "fortified")
        self.assertEqual(state["current"], 1)
        state = store.move(match.code, p1, token1, "fortify", [4, 3])
        self.assertEqual(state["board"][4][3]["strength"], 2)
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, token0, "fortify", [0, 1])
        self.assertEqual(ctx.exception.code, "illegal_move")

    def test_attack_capture_and_repel(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        p1, token1 = players[1]

        # One attacker against a fortified node: 2 vs 2 -> held, turn spent.
        clear_board(match, [(2, 3, 0, 1), (2, 4, 1, 2), (4, 4, 1, 1)], current=0)
        state = store.move(match.code, p0, token0, "attack", [2, 4])
        self.assertEqual(state["last_move"]["result"], "repelled")
        self.assertEqual(state["board"][2][4]["owner"], 1)
        self.assertEqual(state["current"], 1)

        # Add a second adjacent attacker: 3 vs 2 -> captured.
        clear_board(
            match,
            [(2, 3, 0, 1), (1, 4, 0, 1), (2, 4, 1, 2), (4, 4, 1, 1)],
            current=0,
        )
        state = store.move(match.code, p0, token0, "attack", [2, 4])
        self.assertEqual(state["last_move"]["result"], "captured")
        self.assertEqual(state["last_move"]["power"], 3)
        self.assertEqual(state["last_move"]["defense"], 2)
        self.assertEqual(state["board"][2][4]["owner"], 0)
        self.assertEqual(state["board"][2][4]["strength"], 1)
        self.assertEqual(state["players"][0]["nodes"], 1)

    def test_attack_requires_adjacency(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        clear_board(match, [(0, 0, 0, 2), (4, 4, 1, 1)], current=0)
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, token0, "attack", [4, 4])
        self.assertEqual(ctx.exception.code, "illegal_move")


class TestVictory(unittest.TestCase):
    def test_claim_third_node_wins(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        clear_board(
            match,
            [
                (0, 2, 0, 1),
                (2, 0, 0, 1),
                (2, 3, 0, 1),
                (4, 4, 1, 1),
            ],
            current=0,
        )
        state = store.move(match.code, p0, token0, "claim", [2, 4])
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["winner"], 0)
        self.assertEqual(state["finish_code"], "nodes")
        self.assertIn("signal nodes", state["finish_reason"])
        self.assertEqual(state["players"][0]["nodes"], 3)
        self.assertEqual(state["turn_seconds_left"], 0)

    def test_capture_third_node_wins(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        clear_board(
            match,
            [
                (0, 2, 0, 1),
                (2, 0, 0, 1),
                (2, 3, 0, 1),
                (1, 4, 0, 1),
                (2, 4, 1, 2),
                (4, 4, 1, 1),
            ],
            current=0,
        )
        state = store.move(match.code, p0, token0, "attack", [2, 4])
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["winner"], 0)
        self.assertEqual(state["finish_code"], "nodes")
        self.assertEqual(state["board"][2][4]["owner"], 0)

    def test_node_winner_is_ranked_first_even_with_lower_score(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        p0_cells = {
            (0, 2): 1,
            (2, 0): 1,
            (2, 3): 1,
            (1, 4): 1,
        }
        p1_cells: dict[tuple[int, int], int] = {(2, 4): 2}
        # Give player 1 a large point lead from non-node cells. Player 0 still
        # wins instantly by capturing a third node.
        for row in range(5):
            for col in range(5):
                if (row, col) in NODE_CELLS or (row, col) in p0_cells:
                    continue
                if len(p1_cells) >= 16:
                    continue
                p1_cells[(row, col)] = 1
        placements = [
            (row, col, 0, strength) for (row, col), strength in p0_cells.items()
        ] + [
            (row, col, 1, strength) for (row, col), strength in p1_cells.items()
        ]
        clear_board(match, placements, current=0)
        state = store.move(match.code, p0, token0, "attack", [2, 4])
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["winner"], 0)
        self.assertEqual(state["finish_code"], "nodes")
        self.assertEqual(state["ranking"][0]["player"], 0)
        self.assertEqual(state["players"][0]["rank"], 1)

    def test_last_player_standing(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        clear_board(
            match,
            [(4, 3, 0, 1), (3, 4, 0, 1), (4, 4, 1, 1)],
            current=0,
        )
        state = store.move(match.code, p0, token0, "attack", [4, 4])
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["winner"], 0)
        self.assertEqual(state["finish_code"], "last_standing")
        self.assertFalse(state["players"][1]["alive"])
        self.assertEqual(state["players"][1]["cells"], 0)
        self.assertIn("last player standing", state["finish_reason"])

    def test_time_limit_and_scoring(self) -> None:
        store, match, clock, players = make_started(2)
        p0, _ = players[0]
        clock.advance(MATCH_SECONDS + 1)
        clear_board(
            match,
            [
                (2, 0, 0, 1),  # node + cell = 4 points
                (0, 0, 0, 1),
                (4, 4, 1, 1),
                (3, 4, 1, 1),
                (3, 3, 1, 1),
            ],
            current=0,
        )
        # The clock is already past the cap; reading state finalizes the match.
        state = store.get_state(match.code, p0)
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["finish_code"], "time")
        self.assertEqual(state["winner"], 0)
        self.assertEqual(state["players"][0]["score"], 5)
        self.assertEqual(state["players"][1]["score"], 3)
        self.assertEqual(state["match_seconds_left"], 0)

    def test_time_tiebreak_prefers_nodes(self) -> None:
        store, match, clock, players = make_started(2)
        p0, _ = players[0]
        clear_board(
            match,
            [
                (0, 0, 0, 1),
                (0, 1, 0, 1),
                (1, 0, 0, 1),
                (1, 1, 0, 1),  # 4 points, 0 nodes
                (2, 0, 1, 1),  # node + cell = 4 points, 1 node
            ],
            current=0,
        )
        clock.advance(MATCH_SECONDS + 1)
        state = store.get_state(match.code, p0)
        self.assertEqual(state["winner"], 1)
        self.assertEqual(state["players"][0]["score"], 4)
        self.assertEqual(state["players"][1]["score"], 4)
        self.assertEqual(state["finish_code"], "time")
        self.assertEqual(state["ranking"][0]["player"], 1)

    def test_turn_timeout_auto_plays(self) -> None:
        store, match, clock, players = make_started(2)
        p0, _ = players[0]
        clock.advance(TURN_SECONDS + 1)
        state = store.get_state(match.code, p0)
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["current"], 1)
        self.assertEqual(state["move_count"], 1)
        self.assertTrue(state["moves"][0]["auto"])
        self.assertEqual(state["moves"][0]["player"], 0)

    def test_move_at_cap_is_rejected(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        clock.advance(MATCH_SECONDS + 1)
        with self.assertRaises(GameError) as ctx:
            store.move(match.code, p0, token0, "claim", [0, 1])
        self.assertEqual(ctx.exception.code, "match_over")
        state = store.get_state(match.code, p0)
        self.assertEqual(state["status"], "finished")


class TestBots(unittest.TestCase):
    def test_add_bots_and_autoplay(self) -> None:
        clock = FakeClock()
        store = GameStore(clock=clock)
        match, p0, token0 = store.create_match("Ada", 3)
        state = store.add_bots(match.code, p0, token0, 2)
        self.assertEqual(state["joined"], 3)
        self.assertEqual(sum(1 for p in state["players"] if p["bot"]), 2)
        state = store.start_match(match.code, p0, token0)
        self.assertEqual(state["current"], 0)

        state = store.move(match.code, p0, token0, "claim", [0, 1])
        # Two bots play immediately, then it is the human's turn again.
        self.assertEqual(state["current"], 0)
        self.assertEqual(state["move_count"], 3)
        bot_moves = [move for move in state["moves"] if move["bot"]]
        self.assertEqual(len(bot_moves), 2)
        self.assertTrue(all(move["result"] in ("claimed", "fortified", "captured", "repelled")
                            for move in bot_moves))

    def test_bot_host_only_and_lobby_only(self) -> None:
        clock = FakeClock()
        store = GameStore(clock=clock)
        match, p0, token0 = store.create_match("Ada", 4)
        _, p1, token1 = store.join_match(match.code, "Bo")
        with self.assertRaises(GameError) as ctx:
            store.add_bots(match.code, p1, token1, 1)
        self.assertEqual(ctx.exception.code, "not_host")
        state = store.add_bots(match.code, p0, token0, 3)
        self.assertEqual(state["joined"], 4)
        with self.assertRaises(GameError) as ctx:
            store.add_bots(match.code, p0, token0, 1)
        self.assertEqual(ctx.exception.code, "match_full")
        state = store.start_match(match.code, p0, token0)
        with self.assertRaises(GameError) as ctx:
            store.add_bots(match.code, p0, token0, 1)
        self.assertEqual(ctx.exception.code, "wrong_status")

    def test_choose_move_is_always_legal(self) -> None:
        store, match, clock, players = make_started(2)
        for seed in range(8):
            move = choose_move(match, 0, random.Random(seed))
            self.assertIsNotNone(move)
            legal = {(item["action"], tuple(item["target"])) for item in match.legal_moves(0)}
            self.assertIn((move["action"], tuple(move["target"])), legal)

    def test_bot_only_match_finishes_before_time_cap(self) -> None:
        clock = FakeClock()
        store = GameStore(clock=clock)
        match, p0, token0 = store.create_match("Alpha", 2)
        _, p1, token1 = store.join_match(match.code, "Beta")
        store.start_match(match.code, p0, token0)
        match.players[p0].bot = True
        match.players[p1].bot = True
        match.bot_rng = random.Random(7)
        now = match.started_at
        while match.status == "active" and now < match.deadline:
            match.pump(now, max_actions=1)
            now += 1.0
        if match.status == "active":
            match.pump(now, max_actions=1)
        self.assertEqual(match.status, "finished")
        self.assertEqual(match.finish_code, "nodes")
        self.assertLess(len(match.move_log), 100)


class TestReplay(unittest.TestCase):
    def test_replay_contains_frames_and_no_tokens(self) -> None:
        store, match, clock, players = make_started(2)
        p0, token0 = players[0]
        p1, token1 = players[1]
        store.move(match.code, p0, token0, "claim", [0, 1])
        store.move(match.code, p1, token1, "claim", [4, 3])
        replay = store.replay(match.code)
        self.assertEqual(replay["frame_count"], 2)
        self.assertEqual(len(replay["frames"]), 2)
        self.assertEqual(replay["frames"][0]["action"], "claim")
        self.assertEqual(replay["frames"][0]["player"], 0)
        self.assertEqual(replay["frames"][1]["player"], 1)
        self.assertEqual(replay["initial_board"][0][0]["owner"], 0)
        self.assertEqual(replay["initial_board"][0][1]["owner"], None)
        # Each frame has its own board snapshot, including the final frame.
        self.assertEqual(replay["frames"][1]["board"][4][3]["owner"], 1)
        dumped = json.dumps(replay)
        self.assertNotIn("token", dumped)
        self.assertNotIn(token0, dumped)
        self.assertNotIn(token1, dumped)


class TestApiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = GameStore()
        cls.server = GameHTTPServer(("127.0.0.1", 0), GameHandler, cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def raw_get(self, path: str) -> tuple[int, dict[str, str], str]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            raw = response.read()
            return (
                response.status,
                dict(response.getheaders()),
                raw.decode("utf-8", errors="replace"),
            )
        finally:
            conn.close()

    def request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict[str, str], dict | None]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            response_headers = dict(response.getheaders())
            data = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, response_headers, data
        finally:
            conn.close()

    def test_preflight_and_health(self) -> None:
        status, headers, _ = self.request("OPTIONS", "/api/move")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("POST", headers.get("Access-Control-Allow-Methods", ""))
        status, headers, data = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_full_http_flow(self) -> None:
        status, headers, created = self.request(
            "POST", "/api/create", {"name": "Smoke", "seats": 2}
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(created["player"], 0)
        match = created["match"]
        token0 = created["token"]

        status, _, joined = self.request(
            "POST", "/api/join", {"match": match, "name": "Tester"}
        )
        self.assertEqual(status, 201)
        token1 = joined["token"]
        self.assertEqual(joined["player"], 1)

        status, _, started = self.request(
            "POST", "/api/start", {"match": match, "player": 0, "token": token0}
        )
        self.assertEqual(status, 200)
        self.assertEqual(started["status"], "active")
        self.assertEqual(started["current"], 0)

        status, _, error = self.request(
            "POST",
            "/api/move",
            {"match": match, "player": 0, "token": "nope", "action": "claim", "target": [0, 1]},
        )
        self.assertEqual(status, 401)
        self.assertEqual(error["code"], "bad_token")

        status, _, error = self.request(
            "POST",
            "/api/move",
            {"match": match, "player": 1, "token": token1, "action": "claim", "target": [4, 3]},
        )
        self.assertEqual(status, 403)
        self.assertEqual(error["code"], "not_your_turn")

        status, _, state = self.request(
            "POST",
            "/api/move",
            {"match": match, "player": 0, "token": token0, "action": "claim", "target": [0, 1]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["current"], 1)
        self.assertEqual(state["move_count"], 1)

        status, _, state = self.request("GET", f"/api/state?match={match}&player=0")
        self.assertEqual(status, 200)
        self.assertEqual(state["status"], "active")

        status, _, replay = self.request("GET", f"/api/replay?match={match}")
        self.assertEqual(status, 200)
        self.assertEqual(replay["frame_count"], 1)
        self.assertNotIn("token", json.dumps(replay))

        status, headers, html = self.raw_get("/")
        self.assertEqual(status, 200)
        self.assertIn("Signal Siege", html)
        self.assertIn("text/html", headers.get("Content-Type", ""))

    def test_static_and_bot_endpoint(self) -> None:
        status, headers, _ = self.raw_get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))

        status, _, created = self.request(
            "POST", "/api/create", {"name": "Solo", "seats": 2}
        )
        match = created["match"]
        status, _, state = self.request(
            "POST",
            "/api/bot",
            {"match": match, "player": 0, "token": created["token"], "count": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(state["players"]), 2)
        self.assertTrue(state["players"][1]["bot"])

        status, _, state = self.request(
            "POST",
            "/api/start",
            {"match": match, "player": 0, "token": created["token"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["status"], "active")

    def test_json_errors_are_clear_and_cors(self) -> None:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request(
                "POST",
                "/api/create",
                body=b"{",
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            raw = response.read()
            headers = dict(response.getheaders())
        finally:
            conn.close()
        self.assertEqual(response.status, 400)
        payload = json.loads(raw)
        self.assertEqual(payload["code"], "invalid_json")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

        status, _, payload = self.request("POST", "/api/create", {"seats": 2})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_name")

        status, _, payload = self.request("GET", "/api/state?match=UNKNOWN")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "match_not_found")


if __name__ == "__main__":
    unittest.main()
