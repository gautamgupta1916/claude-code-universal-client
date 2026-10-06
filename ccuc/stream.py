"""Translate Claude CLI stream-json NDJSON events into client WS frames.

Ported from smart-gateway-extractor's `_log_stream_event`, but emitting frames
instead of logging. Control-signal events (system/init, result, rate_limit_event)
are handled by the driver, not here.

When `include_partial` is True, assistant text arrives as `stream_event`
text deltas, so the full `assistant` event's text blocks are skipped to avoid
duplication (we still forward its tool_use blocks). When False, we forward the
`assistant` event's text/thinking blocks directly.
"""

from __future__ import annotations

from ccuc import protocol


def _content_preview(content, limit: int = 8000) -> str:
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content") or "")
            else:
                parts.append(str(b))
        s = "\n".join(str(p) for p in parts if p)
    else:
        s = str(content)
    return s[:limit]


def translate(session_id: str, obj: dict, include_partial: bool) -> list[dict]:
    t = obj.get("type")
    frames: list[dict] = []

    if t == "stream_event":
        ev = obj.get("event", {})
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {})
            dt = delta.get("type")
            if dt == "text_delta":
                text = delta.get("text", "")
                if text:
                    frames.append(protocol.assistant_delta(session_id, text))
            elif dt == "thinking_delta":
                text = delta.get("thinking") or delta.get("text", "")
                if text:
                    frames.append(protocol.assistant_thinking(session_id, text))
            # input_json_delta (streamed tool args) intentionally skipped;
            # the full tool input arrives in the `assistant` event below.
        return frames

    if t == "assistant":
        for block in obj.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "tool_use":
                frames.append(
                    protocol.tool_use(session_id, block.get("id"), block.get("name"), block.get("input", {}))
                )
            elif bt == "text":
                # Always emit the final, complete text of a message. With partial
                # messages on, assistant.delta carries the live stream and this is
                # the authoritative final; with them off, this is the only text.
                txt = block.get("text", "")
                if txt:
                    frames.append(protocol.assistant_text(session_id, txt))
            elif bt == "thinking" and not include_partial:
                th = block.get("thinking") or ""
                if th:
                    frames.append(protocol.assistant_thinking(session_id, th))
        return frames

    if t == "user":
        content = obj.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    frames.append(
                        protocol.tool_result(
                            session_id,
                            block.get("tool_use_id"),
                            bool(block.get("is_error", False)),
                            _content_preview(block.get("content", "")),
                        )
                    )
        return frames

    if t == "error":
        err = obj.get("error", obj)
        msg = err.get("message") if isinstance(err, dict) else str(err)
        frames.append(protocol.error("agent_error", msg or "unknown error", session=session_id))
        return frames

    return frames
