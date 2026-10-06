from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """CCUC settings. Env vars are prefixed CCUC_ and may be set in a .env file."""

    model_config = SettingsConfigDict(env_prefix="CCUC_", env_file=".env", extra="ignore")

    # server
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # Claude CLI backend
    claude_binary: str = "claude"
    default_model: str = "sonnet"
    node_heap_mb: int = 8192
    include_partial_messages: bool = True

    # session behavior mode
    default_mode: str = "chat"       # "chat" = no tools, inline answers (like the Messages API); "agent" = full-agentic
    chat_system_prompt: str = (
        "You are running in chat mode with no tools available — you cannot read, "
        "write, or execute anything. Answer the user directly and completely in your "
        "reply, including any code inline as fenced markdown code blocks. Do not say "
        "you will create a file or run a command; just give the answer."
    )

    # one-shot HTTP mode (POST /v1/agent/completions)
    http_mode_enabled: bool = True
    http_turn_timeout_s: float = 300.0   # a blocking HTTP call must not hang like a WS turn can

    # storage
    data_dir: str = "./.ccuc"

    # timeouts (seconds)
    turn_timeout_s: int = 1800
    idle_ttl_s: int = 900            # idle-suspend a warm session after this
    auth_timeout_s: float = 5.0
    heartbeat_s: float = 20.0
    max_queue_wait_s: float = 30.0   # bounded wait for a seat slot before rejecting
    sweep_interval_s: float = 30.0   # how often the idle/stale/disk sweeper runs
    disk_cleanup_hours: int = 72     # remove terminal ephemeral session dirs older than this
    stale_ttl_s: int = 21600         # reap SUSPENDED sessions idle beyond this (6h)

    # optional child resource limits (0 = off; agentic workloads usually need headroom)
    rlimit_nproc: int = 0
    rlimit_cpu_s: int = 0

    # seats
    seat_dirs: list[str] = Field(default_factory=list)
    seat_max_concurrency: int = 1

    # bundles (skills + global MCP shipped inside the app)
    bundles_dir: str = "./bundles"
    default_skill_packs: list[str] = Field(default_factory=lambda: ["default"])
    shared_mcp_enabled: bool = True

    # auth
    dev_api_key: str = "devkey"          # local dev shortcut; set "" to disable
    key_pepper: str = ""                  # HMAC pepper for key hashing; empty = persisted file under data_dir

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def bundles_path(self) -> Path:
        return Path(self.bundles_dir).expanduser().resolve()

    @property
    def rendered_mcp_path(self) -> Path:
        return self.data_path / "mcp.rendered.json"

    @property
    def sessions_dir(self) -> Path:
        return self.data_path / "sessions"

    @property
    def db_path(self) -> Path:
        return self.data_path / "ccuc.db"
