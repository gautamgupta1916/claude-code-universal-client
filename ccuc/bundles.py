"""App-shipped bundles: skill packs (per-session, opt-in) and global MCP.

Skills are copied into each session's isolated working dir so the Claude Code CLI
discovers them from `<cwd>/.claude/skills/` (confirmed: `--add-dir` does NOT trigger
skill discovery). MCP is rendered once at startup and passed to every session.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def skills_root(bundles_dir: Path) -> Path:
    return bundles_dir / "skills"


def list_skill_packs(bundles_dir: Path) -> list[str]:
    root = skills_root(bundles_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def compose_skills(work_dir: Path, packs: list[str], bundles_dir: Path) -> tuple[list[str], list[str]]:
    """Copy each requested pack's skills into <work_dir>/.claude/skills/<pack>__<skill>/.

    Returns (composed_skill_dir_names, errors). Pack-prefixing avoids cross-pack
    name collisions. Copy (not symlink) for robust discovery.
    """
    composed: list[str] = []
    errors: list[str] = []
    root = skills_root(bundles_dir)
    dest_root = work_dir / ".claude" / "skills"

    for pack in packs:
        pack_dir = root / pack
        if not pack_dir.is_dir():
            errors.append(f"unknown skill pack: {pack}")
            continue
        found = 0
        for skill_dir in sorted(pack_dir.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
                continue
            dest = dest_root / f"{pack}__{skill_dir.name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
            composed.append(dest.name)
            found += 1
        if not found:
            errors.append(f"skill pack has no skills: {pack}")

    if composed:
        logger.info("composed %d skill(s) into %s: %s", len(composed), dest_root, ", ".join(composed))
    return composed, errors


def render_shared_mcp(bundles_dir: Path, out_path: Path) -> Optional[str]:
    """Render bundles/mcp/mcp.json (substituting ${SERVERS_DIR}/${BUNDLES_DIR}) to out_path.

    Returns the rendered path (str) if it declares servers, else None.
    """
    template = bundles_dir / "mcp" / "mcp.json"
    if not template.is_file():
        return None
    raw = template.read_text()
    raw = raw.replace("${SERVERS_DIR}", str(bundles_dir / "mcp" / "servers"))
    raw = raw.replace("${BUNDLES_DIR}", str(bundles_dir))
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("invalid bundles/mcp/mcp.json: %s", e)
        return None
    if not cfg.get("mcpServers"):
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=2))
    logger.info("rendered shared MCP -> %s (%d server(s))", out_path, len(cfg["mcpServers"]))
    return str(out_path)
