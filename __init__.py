"""hermes-agent publisher plugin for HookBus.

Emits lifecycle events to a HookBus endpoint and enforces subscriber verdicts.

Hooks registered:
- pre_tool_call  -> PreToolUse event (sync, can block)
- post_tool_call -> PostToolUse event (observation)
- post_api_request -> PostLLMCall event (carries exact model + token usage)

Tool events are tagged with the most recently seen model/provider from the
post_api_request hook, so even subscribers that only see tool events get
model attribution.

Envelope matches the HookBus event schema used by Claude Code, Cursor, Amp,
OpenClaw, Claude Agent SDK, and OpenAI Agents SDK publishers.

Environment:
    HOOKBUS_URL       default http://localhost:18800/event
    HOOKBUS_TOKEN     bearer token, optional
    HOOKBUS_SOURCE    default 'hermes-agent'
    HOOKBUS_TIMEOUT   HTTP timeout in seconds, default 10
    HOOKBUS_FAIL_MODE 'closed' (default for hermes, fail-safe deny) or 'open'
    HOOKBUS_DEBUG     '1' to promote plugin logger to DEBUG level

Licence: MIT. Copyright 2026 Agentic Thinking Limited.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

__version__ = "0.2.1"

logger = logging.getLogger(__name__)
if os.environ.get("HOOKBUS_DEBUG", "") == "1":
    logger.setLevel(logging.DEBUG)

_DEFAULT_URL = "http://localhost:18800/event"
_DEFAULT_TIMEOUT = 10
_DEFAULT_FAIL_MODE = "closed"  # fail-safe: bus unreachable = deny. Set HOOKBUS_FAIL_MODE=open to allow on bus downtime (dev only).


# Cached from the most recent post_api_request so tool-call events can be
# tagged with the model that selected them. Reset per process.
_LAST_MODEL: str = ""
_LAST_PROVIDER: str = ""


def _config() -> Dict[str, Any]:
    fail_mode = os.environ.get("HOOKBUS_FAIL_MODE", _DEFAULT_FAIL_MODE).strip().lower()
    if fail_mode not in ("open", "closed"):
        fail_mode = _DEFAULT_FAIL_MODE
    return {
        "url": os.environ.get("HOOKBUS_URL", _DEFAULT_URL),
        "timeout": int(os.environ.get("HOOKBUS_TIMEOUT", str(_DEFAULT_TIMEOUT))),
        "fail_mode": fail_mode,
        "source": os.environ.get("HOOKBUS_SOURCE", "hermes-agent"),
    }


def _validate_startup_config() -> None:
    """Warn on obviously-broken config at load time. Never raises."""
    cfg = _config()
    try:
        parsed = urlparse(cfg["url"])
        if parsed.scheme not in ("http", "https"):
            logger.error(
                "HOOKBUS_URL has unsupported scheme '%s', only http/https allowed. All events will fail.",
                parsed.scheme,
            )
        elif not parsed.netloc:
            logger.error("HOOKBUS_URL missing host: %s", cfg["url"])
    except Exception as exc:
        logger.error("HOOKBUS_URL is not a valid URL (%s): %s", cfg["url"], exc)
    if not os.environ.get("HOOKBUS_TOKEN", "").strip():
        logger.warning("HOOKBUS_TOKEN is empty; authenticated buses will reject requests")
    logger.debug(
        "hookbus-hermes v%s loaded (source=%s, fail_mode=%s, bus=%s)",
        __version__, cfg["source"], cfg["fail_mode"], cfg["url"],
    )


_validate_startup_config()


def _post_event(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST the event envelope to HookBus. Returns decision dict or None on failure."""
    cfg = _config()

    # Envelope serialisation guard: circular refs in tool_input should not crash the plugin.
    try:
        data = json.dumps(envelope).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.warning("hookbus envelope serialisation failed: %s", exc)
        return None

    headers = {"Content-Type": "application/json"}
    token = os.environ.get("HOOKBUS_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        cfg["url"],
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" not in ctype:
                logger.warning(
                    "hookbus returned non-JSON content-type '%s' for %s",
                    ctype, envelope.get("event_type", "?"),
                )
                return None
            raw = resp.read()
            try:
                parsed = json.loads(raw)
            except Exception as exc:
                logger.warning("hookbus response JSON parse failed: %s", exc)
                return None
            if not isinstance(parsed, dict):
                logger.warning("hookbus response is not a JSON object")
                return None
            return parsed
    except Exception as exc:
        logger.warning("hookbus post failed: %s", exc)
        return None


def _merge_meta(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Merge metadata, auto-injecting cached model/provider where absent."""
    merged = {**base, **extra}
    if _LAST_MODEL and "model" not in merged:
        merged["model"] = _LAST_MODEL
    if _LAST_PROVIDER and "provider" not in merged:
        merged["provider"] = _LAST_PROVIDER
    return merged


def _build_envelope(
    event_type: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
    task_id: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = _config()
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": cfg["source"],
        "session_id": session_id or "default",
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "metadata": _merge_meta({
            "publisher": "hookbus-hermes-publisher",
            "publisher_version": __version__,
            "tool_call_id": tool_call_id,
            "task_id": task_id,
        }, extra or {}),
    }


# ---------------------------------------------------------------------------
# hermes-agent hook callbacks
# ---------------------------------------------------------------------------

def on_pre_tool_call(
    tool_name: str,
    args: Dict[str, Any],
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_ignore: Any,
) -> Optional[Dict[str, Any]]:
    """Called by hermes-agent before a tool executes.

    Emits PreToolUse to HookBus. If any subscriber returns `deny`, block.
    """
    envelope = _build_envelope(
        event_type="PreToolUse",
        tool_name=tool_name,
        tool_input=args or {},
        session_id=session_id,
        tool_call_id=tool_call_id,
        task_id=task_id,
    )

    verdict = _post_event(envelope)
    cfg = _config()

    if verdict is None:
        if cfg["fail_mode"] == "closed":
            return {
                "action": "block",
                "message": "HookBus unreachable and fail_mode=closed, tool call denied.",
            }
        return None  # fail-open: allow

    decision = str(verdict.get("decision", "allow")).lower()
    reason = verdict.get("reason", "") or ""

    if decision == "deny":
        return {
            "action": "block",
            "message": reason or "Blocked by HookBus subscriber (no reason given).",
        }
    if decision not in ("allow", "ask"):
        logger.warning("hookbus returned unknown decision '%s' for PreToolUse, defaulting to allow", decision)
    return None


def on_post_tool_call(
    tool_name: str,
    args: Dict[str, Any],
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_ignore: Any,
) -> None:
    """Emit PostToolUse, observation only."""
    envelope = _build_envelope(
        event_type="PostToolUse",
        tool_name=tool_name,
        tool_input=args or {},
        session_id=session_id,
        tool_call_id=tool_call_id,
        task_id=task_id,
        extra={"result_excerpt": str(result)[:500] if result is not None else ""},
    )
    _post_event(envelope)
    return None


def on_post_api_request(
    model: str = "",
    provider: str = "",
    usage: Optional[Dict[str, Any]] = None,
    api_duration: float = 0.0,
    session_id: str = "",
    task_id: str = "",
    response_model: str = "",
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    **_ignore: Any,
) -> None:
    """Called by hermes after each LLM API round-trip.

    Captures exact token usage from hermes' normalised usage dict, emits a
    PostLLMCall event so TokenGuard and other cost-tracking subscribers record
    real spend, and caches the model so subsequent tool events can be tagged
    with the model that selected them.
    """
    global _LAST_MODEL, _LAST_PROVIDER
    _LAST_MODEL = response_model or model or ""
    _LAST_PROVIDER = provider or ""

    u = usage or {}
    tok_in = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    tok_out = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    total = int(u.get("total_tokens") or (tok_in + tok_out))

    envelope = _build_envelope(
        event_type="PostLLMCall",
        tool_name="llm.api_request",
        tool_input={},
        session_id=session_id,
        task_id=task_id,
        extra={
            "model": _LAST_MODEL,
            "provider": _LAST_PROVIDER,
            "tokens_input": tok_in,
            "tokens_output": tok_out,
            "total_tokens": total,
            "api_duration_ms": int(api_duration * 1000) if api_duration else 0,
            "tool_calls_emitted": assistant_tool_call_count,
            "assistant_content_chars": assistant_content_chars,
        },
    )
    _post_event(envelope)
    return None


# ---------------------------------------------------------------------------
# hermes-agent plugin entry point
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Called by hermes-agent's plugin loader at startup."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_api_request", on_post_api_request)
    cfg = _config()
    logger.info(
        "hookbus-publisher %s registered: url=%s fail_mode=%s",
        __version__, cfg["url"], cfg["fail_mode"],
    )
