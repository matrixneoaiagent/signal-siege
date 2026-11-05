# Signal Siege

**A 2–4 player, 8-minute turn-based turf war for humans and AI agents.**

Signal Siege is a small browser strategy game where every turn is a real choice: expand, fortify, or attack. Humans start a match in one click, fill empty seats with bots, and share a match code; agents join the same matches through a tiny documented HTTP/JSON API. It is one Python process with no external dependencies and no login.

> **Pitch:** Signal Siege is a fast 2–4 player turf war where you claim, fortify, and attack across a 5×5 grid to control three of four signal nodes before an 8-minute clock runs out. One click starts a no-login browser match against a bot, while the same match is fully playable by an AI agent through a documented HTTP/JSON API. Small enough to learn in a minute, with a real decision every turn.

## Rules

Signal Siege is played by 2–4 players on a 5×5 grid; each player starts in a corner with one fortified home cell (strength 2), and on your turn you take exactly one action — claim an empty cell orthogonally adjacent to your territory, fortify one of your own strength-1 cells to strength 2, or attack an adjacent enemy cell, where attack power is 1 plus the number of your cells orthogonally touching the target and the attack captures only if that power is greater than the target's strength (a failed attack still spends your turn); the four signal nodes are the side-midpoint cells and the first player to own any three of them wins instantly, while if the 8-minute match clock expires the player with the highest score (1 point per owned cell plus 3 per owned node) wins, with owned nodes, total cell strength, and then earliest seat as tiebreakers; a player who loses their last cell is eliminated and the last player standing wins, and a 30-second turn clock automatically plays a heuristic move for a player who does not act.

## Quick start

Requires Python 3.12 (standard library only).

```bash
# from the repository root (the directory containing this README)
./run.sh
# or: python3 game_server.py
```

Open **http://127.0.0.1:3100** and click **Play vs bot**. The quick-play flow creates a 2-seat match, adds one server-side bot, and starts it — no install, account, or login. Create a match to get a code, share the link, or add bots to empty seats.

To expose the server on a host or LAN (for a public URL, put it behind HTTPS/reverse proxy):

```bash
./run.sh --host 0.0.0.0 --port 3100
```

Matches are held in memory and reset on restart. The browser keeps your player token in `localStorage` so refreshing rejoins your seat.

## Tests

```bash
python3 -m unittest -v test_game.py
```

26 tests cover the game engine, turn/token validation, combat, node/time/elimination wins, final ranking, the turn clock, bots, replay, and a real HTTP smoke test including CORS and JSON errors.

## API overview

Base URL: `http://127.0.0.1:3100`

All API responses are JSON. CORS is open (`Access-Control-Allow-Origin: *`) so browser and agent clients can call the API from another origin. Reads are public; token-authenticated writes are only needed to start a match, add bots, or submit a move. Tokens are returned by create/join and are **never** included in state or replay responses.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/create` | Create a match and take seat 0 (host). |
| `POST` | `/api/join` | Join a lobby by code. |
| `POST` | `/api/start` | Host starts the match (2+ players). |
| `POST` | `/api/bot` | Host fills empty seats with server bots. |
| `GET` | `/api/state?match=CODE[&player=ID]` | Full public state and legal moves. |
| `POST` | `/api/move` | Submit one action with token. |
| `GET` | `/api/replay?match=CODE` | Initial board, every move, and board snapshots. |
| `GET` | `/api/health` | Server and match counters. |
| `OPTIONS` | any `/api/*` | CORS preflight. |

Coordinates are `[row, col]` with both values `0..4`; row `0` is the top row and column `0` is column A. The UI labels them A1–E5.

### `POST /api/create`

Request: `{"name": "Ada", "seats": 2}`

`seats` is optional and must be `2`–`4` (default `2`). Names are 1–24 characters and unique within a match. Response `201`:

```json
{"match": "ABC123", "player": 0, "token": "32-hex-chars", "seats": 2}
```

Keep the token secret — it authorizes your moves. The match code is shareable and case-insensitive.

### `POST /api/join`

Request: `{"match": "ABC123", "name": "Grace"}`

Response `201` has the same shape as create. Joining is allowed only while the match is in the lobby and has a free seat. The creator is the host (seat `0`).

### `POST /api/start`

Request: `{"match": "ABC123", "player": 0, "token": "..."}`

Only the host can start, and at least two players must have joined. Unfilled seats simply sit out. Response `200` is the full active state.

### `POST /api/bot`

Request: `{"match": "ABC123", "player": 0, "token": "...", "count": 1}`

Host-only, lobby-only. Fills up to `count` empty seats (default `1`, capped by the number of empty seats) with named heuristic bots. Response `200` is the full state. Bots move automatically after human moves and are also driven by the server's background clock.

### `GET /api/state?match=ABC123&player=0`

Returns the full state. `player` is optional and selects the `you`/`your_turn` perspective; omit it for a spectator view. It must be an existing player ID if supplied. Key fields (trimmed example; the real board is a full 5×5 array):

```json
{
  "match": "ABC123",
  "status": "active",
  "seats": 2,
  "board": [[{"owner": 0, "strength": 2, "node": false}, "..."], "..."],
  "players": [
    {"id": 0, "name": "Ada", "color": "#ff6b6b", "bot": false,
     "alive": true, "cells": 7, "nodes": 2, "strength": 10,
     "score": 13, "rank": 1}
  ],
  "current": 0,
  "turn": 12,
  "your_turn": true,
  "legal": [
    {"action": "claim", "target": [0, 1], "node": false},
    {"action": "attack", "target": [2, 4], "node": true,
     "power": 3, "defense": 2, "captures": true, "defender": 1}
  ],
  "nodes": [[0, 2], [2, 0], [2, 4], [4, 2]],
  "node_target": 3,
  "match_seconds_left": 401,
  "turn_seconds_left": 27,
  "winner": null,
  "finish_reason": null,
  "moves": [
    {"seq": 1, "turn": 1, "player": 0, "player_name": "Ada",
     "action": "claim", "target": [1, 0], "result": "claimed",
     "detail": "Claimed A2", "at": 2.1, "auto": false, "bot": false}
  ]
}
```

`board[row][col]` is always present; `owner` is `null` for empty cells and `strength` is `0` for empty cells, `1` or `2` for owned cells. `legal` is computed for the current player and includes attack `power`, `defense`, and `captures` so clients can preview combat. `moves` contains the most recent 50 moves (without board snapshots); `move_count` is the total. `turn_seconds_left` is the 30-second turn clock; `match_seconds_left` is the 8-minute match clock. `finish_reason` is a human-readable result.

### `POST /api/move`

Request:

```json
{"match": "ABC123", "player": 0, "token": "...",
 "action": "claim", "target": [0, 1]}
```

`action` is `claim`, `fortify`, or `attack`. The server validates the token, that it is your turn, and that the move is legal. Response `200` is the full state after the move (and after any immediate bot replies).

### `GET /api/replay?match=ABC123`

Response:

```json
{
  "match": "ABC123",
  "status": "finished",
  "players": [{"id": 0, "name": "Ada", "color": "#ff6b6b", "bot": false}],
  "initial_board": [[{"owner": null, "strength": 0, "node": false}]],
  "frames": [
    {"seq": 1, "turn": 1, "player": 0, "player_name": "Ada",
     "action": "claim", "target": [1, 0], "result": "claimed",
     "detail": "Claimed A2", "at": 2.1, "auto": false, "bot": false,
     "board": [[{"owner": 0, "strength": 1, "node": false}]]}
  ],
  "frame_count": 1,
  "winner": 0,
  "winner_name": "Ada",
  "finish_reason": "Ada controls 3 of the 4 signal nodes."
}
```

Every frame includes the board after that move, so a client can replay without re-implementing the rules. Frames never contain player tokens.

### Errors

Errors use the matching HTTP status and a stable machine-readable `code`:

```json
{"error": "That cell is already owned.", "code": "illegal_move"}
```

Common codes: `invalid_json`, `invalid_name`, `invalid_seats`, `invalid_target`, `invalid_action`, `illegal_move`, `bad_token`, `not_your_turn`, `not_host`, `match_not_found`, `match_full`, `match_not_joinable`, `not_enough_players`, `match_not_active`, `match_over`, `name_taken`, `wrong_status`, `payload_too_large`.

### cURL walkthrough

```bash
BASE=http://127.0.0.1:3100

# 1. Create a 2-seat match. Note the match code and token.
curl -s -X POST "$BASE/api/create" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Ada","seats":2}'
# {"match":"ABC123","player":0,"token":"TOKEN","seats":2}

# 2. Add one bot and start the match (host token required).
curl -s -X POST "$BASE/api/bot" \
  -H 'Content-Type: application/json' \
  -d '{"match":"ABC123","player":0,"token":"TOKEN","count":1}'

curl -s -X POST "$BASE/api/start" \
  -H 'Content-Type: application/json' \
  -d '{"match":"ABC123","player":0,"token":"TOKEN"}'

# 3. Read state (public; player=0 adds your perspective).
curl -s "$BASE/api/state?match=ABC123&player=0"

# 4. Claim row 0, column 1 (B1). The token is required for moves.
curl -s -X POST "$BASE/api/move" \
  -H 'Content-Type: application/json' \
  -d '{"match":"ABC123","player":0,"token":"TOKEN","action":"claim","target":[0,1]}'

# 5. Watch the replay.
curl -s "$BASE/api/replay?match=ABC123"
```

To play with a second human: create with `"seats":2`, share the code/link, have them `POST /api/join`, then the host calls `/api/start`.

## Sample bot

`bot.py` is a standalone standard-library agent that plays over the HTTP API. It does not import the server.

```bash
# Join an existing lobby by code. The host starts the match.
python3 bot.py --match ABC123 --name Scout

# Create a match as host. It prints the code and auto-starts when a second
# player joins (a human in the browser, or another bot).
python3 bot.py --create --name Scout --seats 2
```

Two bots can play each other in two terminals:

```bash
python3 bot.py --create --name Alpha --seats 2   # terminal 1: prints CODE
python3 bot.py --match CODE --name Beta          # terminal 2
```

Options:

- `--name NAME` — display name (default `Scout`).
- `--seats N` — 2–4 seats when creating (default 2).
- `--base-url URL` — default `http://127.0.0.1:3100`; `SIGNAL_SIEGE_URL` is also read.
- Ctrl+C stops the bot.

The sample policy values node captures, safe captures, and fortifying threatened nodes; it deliberately avoids pointless one-for-one recaptures. The browser's **Add bot** button uses a similar server-side heuristic, and the same heuristic auto-plays a human seat after 30 seconds of inactivity.

## Design notes

- **Four nodes, control any three.** Side-midpoint nodes keep every corner start symmetric and create multiple win routes. The map is a 5×5 grid with home corners; a two-player game starts on opposite corners.
- **Combat is deterministic support.** Attack power is 1 + your orthogonally adjacent cells. A fortified cell needs at least two adjacent attackers to capture. Every point of strength and every adjacent cell matters.
- **No single dominant action.** Claiming adds score and territory but exposes new cells; fortifying protects a cell but scores nothing that turn; attacking can flip a cell or deny a node but spends a turn and can be recaptured. Nodes are worth +3 and a third node wins instantly, so defending and denying them matters.
- **Games finish.** The 8-minute cap resolves scoring, the 30-second clock auto-moves idle humans, and the built-in bot is recapture-aware so it does not stall in endless one-cell trades.
- **Public information.** Boards, scores, and even the current player's legal moves are public. Only tokens are secret.

## Project layout

```
game_server.py     standard-library engine, bots, HTTP/JSON API, static server
static/index.html  self-contained browser UI (CSS + JS, no build step)
bot.py             standalone sample agent / HTTP client
test_game.py       26 engine and HTTP tests
run.sh             launch helper (defaults to 127.0.0.1:3100)
README.md          this file
.gitignore
```

## Known limitations

- **Not deployed.** This repository is a local, self-contained server; it makes no claim to be hosted publicly. Put it behind a TLS reverse proxy and add rate limiting before exposing it on the internet.
- **In-memory only.** Restarting loses all matches. There are no accounts, chat, persistence, ratings, or matchmaking lobby beyond shared codes.
- **Browser seat recovery** relies on `localStorage`. Clearing site data lets you view a match by code but not move as that player; the token is not recoverable.
- **No hidden information or randomness** in the core rules; games are deterministic given the players' choices. The bot adds slight move-selection jitter for variety.
- **Turn timer is wall-clock.** A match can be decided at 8 minutes even if no one is present. The UI polls; the server also runs a background clock ticker.
- **Single process.** A threaded `http.server` is intentionally small, not a production-scale game server. Replay board snapshots are held in memory for each match.
- **Bots are heuristic**, not strong AI. They are meant to fill seats and give a human a lively opponent.
