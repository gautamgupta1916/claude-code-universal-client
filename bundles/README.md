# bundles/

App-shipped extensions available to CCUC sessions. Baked into the Docker image
(`/app/bundles`) and readable/editable right here in the repo.

## skills/  — per-session, opt-in skill packs

```
skills/<pack-name>/<skill-name>/SKILL.md
```

A `SKILL.md` has YAML frontmatter (`name`, `description`) + instructions. To add a
skill, just create a `<skill-name>/` folder with a `SKILL.md` inside a pack.

Clients opt into packs per session:

```json
{"type": "session.open", "skills": ["pgs-skills", "common"]}
```

Each requested pack is **copied** into that session's isolated working directory at
`.claude/skills/<pack>__<skill>/`, so the agent gets exactly those skills while
sessions stay isolated (the source packs here are never mutated). When a client
names no packs, the server's defaults apply (`CCUC_DEFAULT_SKILL_PACKS`, default
`["default"]`).

## mcp/  — global MCP servers (every session)

`mcp/mcp.json` is a template passed to **every** session (`--mcp-config
--strict-mcp-config`). `${SERVERS_DIR}` is substituted with the absolute path to
`mcp/servers/` at startup. `mcp/servers/util_server.py` is a zero-dependency
example (echo/add) — replace with your own servers and list them in `mcp.json`.
