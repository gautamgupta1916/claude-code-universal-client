# CCUC — Demo & Usage Guide

> **Key point about `curl`:** `curl` works for the two **HTTP** endpoints
> (`/health`, `/sessions`). The agent itself is a **WebSocket** (`/v1/agent`) —
> `curl` can't drive it. Use one of: the bundled **`scripts/wsclient.py`** (zero
> install, auto-handles session ids), **Postman** (WebSocket request), or
> **`websocat`** (`brew install websocat`).

---

## 1. Run it natively (uses your machine's `claude` login)

Prereq: `claude auth status` shows you're logged in.

```bash
cd claude-code-universal-client
python3 -m venv .venv && source .venv/bin/activate   # first time
pip install -e .                                       # first time

ccuc            # start the gateway on http://localhost:8080  (Ctrl-C to stop)
```

Defaults: model `sonnet`, dev key `devkey`, data under `./.ccuc`, one ambient seat.

### HTTP (curl)
```bash
curl -s localhost:8080/health   | python3 -m json.tool
curl -s localhost:8080/sessions | python3 -m json.tool   # run while a session is open
```

### Talk to the agent — easiest (bundled client)
```bash
# DEFAULT = chat mode: no tools, answers inline (like the Messages API)
python scripts/wsclient.py --key devkey \
  --message "Give me C++ code for BFS" \
  --message "Explain its time complexity in one sentence."

# AGENT mode: full-agentic — actually creates/edits files and runs commands
python scripts/wsclient.py --key devkey --mode agent \
  --message "Create hello.txt with 'hi from ccuc' then cat it" \
  --message "What did the file say?"

# agent mode + a skill pack (session gets those skills)
python scripts/wsclient.py --key devkey --mode agent --skills pgs-skills --message "greet me"

# interactive chat (type turns, /quit to exit)
python scripts/wsclient.py --key devkey --interactive
```

---

## 2. Run it in Docker (Colima)

```bash
colima start                       # if the Docker daemon isn't up
docker compose build

# A container has no ambient login. For REAL turns, provide a subscription token
# generated on a logged-in machine (skip this to just exercise the plumbing):
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"

docker compose up -d
curl -s localhost:8080/health | python3 -m json.tool

python scripts/wsclient.py --key devkey --mode agent --skills pgs-skills --message "greet me"

docker compose down                # stop
```

Skill packs (`bundles/skills/`) and the global MCP (`bundles/mcp/`) are baked into
the image. Uncomment `./bundles:/app/bundles` in `docker-compose.yml` to edit
packs without rebuilding.

---

## 3. Manage API keys (real keys, not the dev key)

```bash
ccuc-keys create --name acme --root ./workspaces/acme --model sonnet \
  --max-concurrent 3 --max-turns-per-day 500
# -> prints  ccuc_<id>_<secret>   (shown ONCE)

ccuc-keys list
ccuc-keys show   <key_id>
ccuc-keys revoke <key_id>
```

Scope options: `--root` (working-dir jail), `--model`, `--allow-tool`/`--deny-tool`
(repeatable), `--max-concurrent`, `--max-turns-per-day`, `--no-subdir`.

Use a minted key anywhere `devkey` is used: `--key ccuc_<id>_<secret>`.

---

## 4. Driving the WebSocket by hand (Postman / websocat)

Connect to `ws://localhost:8080/v1/agent`. Authenticate with either an
`Authorization: Bearer <key>` header **or** a first `auth` frame. Every message is
one JSON object.

### websocat (closest thing to "curl for WebSockets")
```bash
brew install websocat
websocat -H 'Authorization: Bearer devkey' ws://localhost:8080/v1/agent
```
Then paste frames one per line and read the responses:
```json
{"v":1,"type":"session.open","req_id":"o1","skills":["pgs-skills"]}
```
Copy the `session` id from the `session.ready` reply, then:
```json
{"v":1,"type":"user.message","req_id":"m1","session_id":"<PASTE-ID>","text":"greet me"}
```

### Client → server frames
| frame | fields |
|---|---|
| `auth` | `token` |
| `session.open` | `req_id?`, `mode?` (`chat`\|`agent`), `cwd?`, `model?`, `skills?` (list, agent), `allowed_tools?`, `append_system_prompt?` |
| `user.message` | `session_id`, `text`, `req_id?` |
| `interrupt` | `session_id` — cancel the running turn, keep the process |
| `session.close` | `session_id` — **hard-kill** the claude process + children |
| `ping` | — |

### Server → client frames
`ready` (on connect) · `session.ready` (`session`, `mode`, `seat`, `cwd`, `tools`, `skills`) ·
`assistant.delta` (live text) · `assistant.text` (final) · `tool.use` · `tool.result` ·
`turn.complete` (`stop_reason`, `cost_usd`, `num_turns`, `usage`) · `error` (`code`, `message`) · `pong`

---

## 5. Suggested 60-second demo script

```bash
# terminal 1
ccuc

# terminal 2
curl -s localhost:8080/health | python3 -m json.tool          # gateway + seat status
python scripts/wsclient.py --key devkey --mode agent --skills pgs-skills \
  --message "greet me" \
  --message "now list the files you created"                  # streamed tools + skill + multi-turn
# while that runs, in a 3rd terminal:
curl -s localhost:8080/sessions | python3 -m json.tool        # the live session, its seat + status

# ...and the stateless one-liner, no WebSocket client required:
curl -sS localhost:8080/v1/agent/completions \
  -H 'Authorization: Bearer devkey' -H 'Content-Type: application/json' \
  -d '{"prompt":"What is 17*23?","mode":"chat"}' | python3 -m json.tool
```

---

## Notes / gotchas
- **Two front doors.** `POST /v1/agent/completions` is one call = one session:
  it opens, runs a single turn, kills the process and frees the seat before
  responding. Use it for short self-contained asks. Anything long-running,
  multi-turn, interruptible, or needing live progress goes over the WebSocket —
  the HTTP response has no streaming and its `session_id` is **not** resumable.
  Smoke test: `python scripts/test_rest.py`.
- **`curl` cannot open the WebSocket** — use wsclient.py / Postman / websocat
  (but `curl` is exactly right for the one-shot HTTP endpoint).
- Send **one JSON object per message**; free text (e.g. `hey`) returns a `bad_frame` error by design.
- Default mode is `chat` (no tools, inline answers, like the Messages API). Pass
  `mode:"agent"` (or `--mode agent`) for full-agentic, file-writing behavior;
  `skills` and the global MCP apply only in agent mode.
- In agent mode, `session.open` with no `skills` loads the server default pack (`default`).
- Default `CCUC_SEAT_MAX_CONCURRENCY=1` → a 2nd *concurrent* session returns
  `seat_pool_saturated`. Raise it (`CCUC_SEAT_MAX_CONCURRENCY=4 ccuc`) or add seats
  (`CCUC_SEAT_DIRS`) for more parallelism.
- All settings are `CCUC_*` env vars — see `.env.example`.
