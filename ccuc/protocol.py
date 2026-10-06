"""WebSocket wire protocol: inbound (client->server) pydantic frames and
outbound (server->client) frame builders. All frames carry `v` and `type`."""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter

PROTOCOL_VERSION = 1


# ---------------- Inbound (client -> server) ----------------

class AuthFrame(BaseModel):
    type: Literal["auth"]
    token: str


class SessionOpen(BaseModel):
    type: Literal["session.open"]
    req_id: Optional[str] = None
    cwd: Optional[str] = None
    model: Optional[str] = None
    append_system_prompt: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    disallowed_tools: Optional[list[str]] = None
    skills: Optional[list[str]] = None  # skill-pack names to load; None -> server default packs
    mode: Optional[Literal["chat", "agent"]] = None  # None -> server default_mode
    mcp: Optional[dict] = None


class SessionResume(BaseModel):
    type: Literal["session.resume"]
    req_id: Optional[str] = None
    session_id: str


class UserMessage(BaseModel):
    type: Literal["user.message"]
    req_id: Optional[str] = None
    session_id: str
    text: str


class Interrupt(BaseModel):
    type: Literal["interrupt"]
    session_id: str


class SessionClose(BaseModel):
    type: Literal["session.close"]
    session_id: str


class Ping(BaseModel):
    type: Literal["ping"]


Inbound = Annotated[
    Union[AuthFrame, SessionOpen, SessionResume, UserMessage, Interrupt, SessionClose, Ping],
    Field(discriminator="type"),
]
_inbound_adapter = TypeAdapter(Inbound)


def parse_inbound(raw: str):
    """Parse+validate a client frame. Raises pydantic ValidationError on bad input."""
    return _inbound_adapter.validate_json(raw)


# ---------------- Outbound (server -> client) ----------------

def _frame(type_: str, session: Optional[str] = None, **payload) -> dict:
    f: dict = {"v": PROTOCOL_VERSION, "type": type_}
    if session is not None:
        f["session"] = session
    f.update(payload)
    return f


def hello(limits: Optional[dict] = None) -> dict:
    return _frame("ready", protocol_version=PROTOCOL_VERSION, limits=limits or {})


def session_ready(session, seat, cwd, model, tools=None, mcp_servers=None, req_id=None, skills=None, mode=None) -> dict:
    return _frame(
        "session.ready", session=session, seat=seat, cwd=cwd, model=model,
        tools=tools or [], mcp_servers=mcp_servers or [], skills=skills or [], mode=mode, req_id=req_id,
    )


def session_status(session, status, detail=None) -> dict:
    return _frame("session.status", session=session, status=status, detail=detail)


def assistant_delta(session, text) -> dict:
    return _frame("assistant.delta", session=session, text=text)


def assistant_text(session, text) -> dict:
    return _frame("assistant.text", session=session, text=text)


def assistant_thinking(session, text) -> dict:
    return _frame("assistant.thinking", session=session, text=text)


def tool_use(session, id, name, input) -> dict:
    return _frame("tool.use", session=session, id=id, name=name, input=input)


def tool_result(session, tool_use_id, is_error, content) -> dict:
    return _frame("tool.result", session=session, tool_use_id=tool_use_id, is_error=is_error, content=content)


def turn_complete(session, req_id=None, **result) -> dict:
    return _frame("turn.complete", session=session, req_id=req_id, **result)


def session_closed(session, reason=None) -> dict:
    return _frame("session.closed", session=session, reason=reason)


def error(code, message, session=None, retryable=False, detail=None, req_id=None) -> dict:
    return _frame("error", session=session, code=code, message=message, retryable=retryable, detail=detail, req_id=req_id)


def pong() -> dict:
    return _frame("pong")
