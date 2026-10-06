# claude-code-universal-client (CCUC)

Expose **Claude Code** as an API-key-gated, full-agentic agent backend,
authenticated to Claude via a **pool of OAuth subscription seats**.

Internal services present an API key and drive a live Claude Code session.
Under the hood the gateway keeps one persistent
`claude -p --input-format stream-json --output-format stream-json` process per
active session and bridges it to the client.

## Two ways in

| | **WebSocket** `/v1/agent` | **One-shot HTTP** `POST /v1/agent/completions` |
|---|---|---|
| Shape | stateful, streaming | one request = one session, blocking JSON |
| Turns | many, with `interrupt` and `resume` | exactly one |
| Progress | live `assistant.delta` / `tool.use` frames | nothing until the answer |
| State | survives reconnect | none — no resume, no continuation |
| Use for | long or interactive agent work | "ask a question, get an answer": cron jobs, backend calls, curl |

**Reach for HTTP when the task is short and self-contained. Anything
long-running, multi-turn, or that needs live progress should use the
WebSocket.** Both share the same seat pool, session registry, quotas and audit
log, so `/health`, `/stats` and `/metrics` show one unified view.

> Status: feature-complete for the planned scope — live WS↔stdin/stdout bridge,
> API keys + scopes + cwd jail, per-session **chat/agent mode**, mid-turn interrupt, resume across reconnect,
> per-session **skill packs**, a **global bundled MCP**, **hard-kill close**, a
> **multi-seat pool** with idle-suspend / transparent re-warm + per-key concurrency
> & daily quotas, and **observability** (`/stats`, `/metrics`, SQLite audit log).
> plus a **one-shot HTTP mode** for stateless request/response callers.
> All verified natively and in Docker. See
> `~/.claude/plans/see-this-repo-frolicking-sunset.md` for the full plan.

## Quick start (local, one ambient seat)

Requires Python 3.11+, Node, and the `claude` CLI already logged in
(`claude auth status` should show you're authenticated).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env            # optional; defaults work for local dev

# terminal 1 — run the gateway
ccuc                            # or: python -m ccuc.app

# terminal 2 — talk to it. Default is CHAT mode: no tools, answers inline
# (like the Messages API) — you get code in the reply, not a file on the server.
python scripts/wsclient.py --key devkey \
  --message "Give me C++ code for BFS" \
  --message "Explain its time complexity in one sentence."

# opt into AGENT mode for full-agentic, file-writing behavior:
python scripts/wsclient.py --key devkey --mode agent \
  --message "Create hello.txt containing 'hello from ccuc' then cat it"

# ...or interactive:
python scripts/wsclient.py --key devkey --interactive

# no WebSocket needed — one HTTP call, one answer:
curl -sS localhost:8080/v1/agent/completions \
  -H 'Authorization: Bearer devkey' -H 'Content-Type: application/json' \
  -d '{"prompt":"What is 17*23?","mode":"chat"}' | jq -r .text
```

Session working directories are created under `./.ccuc/sessions/<id>/work`.

## One-shot HTTP mode

`POST /v1/agent/completions` opens a session, runs a single turn, then kills the
process and frees the seat **before** responding. The returned `session_id` is a
correlation handle for the logs and audit trail — it is **not** resumable.

```bash
curl -sS localhost:8080/v1/agent/completions \
  -H 'Authorization: Bearer <key>' -H 'Content-Type: application/json' -d '{
    "prompt": "Create hello.txt with a greeting, then confirm.",
    "mode": "agent",
    "model": "sonnet",
    "skills": ["default"],
    "timeout_s": 120
  }'
```

```json
{
  "session_id": "…", "text": "Done — created hello.txt.",
  "messages": ["…"],
  "tool_calls": [{"id": "toolu_…", "name": "Write", "input": {…},
                  "result": "…", "is_error": false}],
  "stop_reason": "end_turn", "cost_usd": 0.034, "duration_ms": 8123,
  "num_turns": 2, "usage": {…}, "is_error": false,
  "model": "claude-sonnet-5", "mode": "agent", "seat": "seat-00", "skills": []
}
```

Request fields mirror `session.open` (`mode`, `model`, `cwd`, `skills`,
`allowed_tools`, `disallowed_tools`, `append_system_prompt`) plus `prompt` and
`timeout_s` (clamped to `CCUC_HTTP_TURN_TIMEOUT_S`, default 300s — deliberately
much shorter than the 1800s WS turn timeout, since a blocking HTTP call sits
behind proxies).

Errors come back as `{"error": {"code", "message", "retryable", "detail"}}` with
the wire error code mapped to a status: `401` unauthenticated, `403`
model_not_allowed, `400` bad_cwd, `429` too_many_sessions / quota_exceeded,
`503` seat_pool_saturated (with `Retry-After`), `504` turn_timeout, `502`
turn_failed / agent_error.

Caveats worth knowing:

- **Cold start every call** (~2–4s to spawn `claude` and reach init). Chatty or
  latency-sensitive workloads belong on the WebSocket.
- **Shared capacity** — a burst of HTTP calls hits the same per-key
  `max_concurrent_sessions` and seat pool as WS sessions, and gets
  `seat_pool_saturated` once seats are full.
- If the client hangs up mid-request the turn is aborted and the seat released
  within ~1s, rather than running on for nobody.
- Set `CCUC_HTTP_MODE_ENABLED=false` to serve WebSocket only.

Smoke test: `python scripts/test_rest.py [key]`.

## WebSocket protocol (v1)

Connect to `ws://<host>:<port>/v1/agent`, authenticate with either an
`Authorization: Bearer <key>` header or a first frame `{"type":"auth","token":"<key>"}`.

Client → server: `session.open` (optional `mode`, `cwd`, `model`, `skills`, `allowed_tools`),
`user.message`, `interrupt` (cancel the current turn, keep the process), `session.resume`,
`session.close` (hard-kill the claude process + its children; non-resumable), `ping`.
Server → client: `ready`, `session.ready` (reports `mode`, `seat`, `cwd`, `tools`, loaded `skills`),
`assistant.delta`, `assistant.text`, `tool.use`, `tool.result`, `turn.complete`, `error`, `pong`.

## Session modes: `chat` (default) vs `agent`

Each session picks a **mode** at `session.open`, fixed for the session's life:

- **`chat`** (default) — the CLI runs with **no tools** (`--tools ""`), no MCP, no
  skills. The agent answers **inline** in the response stream, like the Anthropic
  Messages API: ask for BFS code and you get the code in `assistant.text`, not a
  file on the server. Zero filesystem side effects.
- **`agent`** — full-agentic Claude Code: Bash/Edit/Write/Read, the global MCP, and
  skill packs. This is the mode that creates/edits files and runs commands.

```json
{"type": "session.open", "mode": "agent", "skills": ["pgs-skills"]}
```

The server default is `CCUC_DEFAULT_MODE` (ships `chat`). `session.ready` echoes the
resolved `mode`. To switch a conversation, open a new session with the other mode
(mid-session switching is intentionally not supported).

## Docker

```bash
docker compose build
# A container has no ambient login — provide a subscription token generated on
# an already-logged-in machine:
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"   # run where `claude` is logged in
docker compose up -d
curl localhost:8080/health

python scripts/wsclient.py --key devkey --message "Create hi.txt with 'hey' then cat it"
```

Alternatively, mount pre-authenticated seats: put one `CLAUDE_CONFIG_DIR` per
folder under `./seats/` (each holding a `.credentials.json` from `claude auth
login`), uncomment the `./seats:/seats:ro` mount, and set
`CCUC_SEAT_DIRS='["/seats/seat-00", ...]'`.

## Skills & MCP bundles

App-shipped extensions live in `bundles/` (baked into the image, browsable in the repo).

**Skill packs** — `bundles/skills/<pack>/<skill>/SKILL.md`. A client opts in per session:

```json
{"type": "session.open", "skills": ["pgs-skills"]}
```

The named packs are copied into that session's isolated working dir
(`.claude/skills/<pack>__<skill>/`), so Claude discovers them while sessions stay
isolated. With no `skills` field, the server default applies
(`CCUC_DEFAULT_SKILL_PACKS`, default `["default"]`). Add a skill by dropping a
`<skill>/SKILL.md` folder into a pack.

**Global MCP** — `bundles/mcp/mcp.json` (+ `servers/`) is passed to *every* session
(`--mcp-config --strict-mcp-config`). The scaffold ships a zero-dependency
`util_server.py` (echo/add); `${SERVERS_DIR}` is resolved to an absolute path at
startup. Add servers to `mcp.json` (Python or Node; both are in the image).

Try it:

```bash
python scripts/wsclient.py --key devkey --skills pgs-skills --message "greet me"
```

See `bundles/README.md` for the full layout.

## Configuration

All settings are env vars prefixed `CCUC_` (see `.env.example`). Notably:
`CCUC_DEFAULT_MODE` (`chat` or `agent`; default `chat`),
`CCUC_SEAT_DIRS` (JSON list of pre-authenticated `CLAUDE_CONFIG_DIR`s; empty =
one ambient seat), `CCUC_DEFAULT_MODEL`, `CCUC_DATA_DIR`, `CCUC_DEV_API_KEY`,
`CCUC_HTTP_MODE_ENABLED` (default `true`), `CCUC_HTTP_TURN_TIMEOUT_S` (default `300`).
