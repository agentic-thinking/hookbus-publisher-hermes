"""hermes-agent publisher plugin for HookBus.

Emits lifecycle events to a HookBus endpoint and enforces subscriber verdicts.

Hooks registered:
- pre_gateway_dispatch -> UserPromptSubmit event (sync, can drop the message)
- pre_api_request -> PreLLMCall event (sync, can block/budget)
- post_api_request -> PostLLMCall event (carries model, tokens, reasoning_content, response_content)
- pre_tool_call  -> PreToolUse event (sync, can block)
- post_tool_call -> PostToolUse event (observation)

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
import re
import threading
import uuid
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

__version__ = "0.3.0"

logger = logging.getLogger(__name__)
if os.environ.get("HOOKBUS_DEBUG", "") == "1":
    logger.setLevel(logging.DEBUG)

_DEFAULT_URL = "http://localhost:18800/event"
_DEFAULT_TIMEOUT = 10
_DEFAULT_FAIL_MODE = "closed"  # fail-safe: bus unreachable = deny. Set HOOKBUS_FAIL_MODE=open to allow on bus downtime (dev only).

SCHEMA_VERSION = 1


# Cached from the most recent post_api_request so tool-call events can be
# tagged with the model that selected them. Reset per process.
_LAST_MODEL: str = ""
_LAST_PROVIDER: str = ""

# Per-session memory of the last user_message seen on a pre_llm_call. Used to
# detect a fresh user turn (initial or mid-session follow-up) so we publish
# UserPromptSubmit + inject CRE's KB context once per user turn, not on every
# LLM call inside a tool-use loop.
_LAST_USER_MESSAGE: Dict[str, str] = {}

# correlation_id cache: keyed by tool_call_id (tools) or task_id (LLM calls)
_CORRELATIONS: "OrderedDict[str, str]" = OrderedDict()
_STATE_LOCK = threading.RLock()
_MAX_TRACKED_STATE = 2048


def _trim_ordered_dict(data: OrderedDict, max_size: int = _MAX_TRACKED_STATE) -> None:
    """Bound plugin bookkeeping so abandoned hook pairs cannot leak forever."""
    while len(data) > max_size:
        data.popitem(last=False)


def _put_correlation(key: str, correlation_id: str) -> None:
    if not key:
        return
    with _STATE_LOCK:
        _CORRELATIONS[key] = correlation_id
        _CORRELATIONS.move_to_end(key)
        _trim_ordered_dict(_CORRELATIONS)


def _pop_correlation(key: str) -> str:
    if not key:
        return ""
    with _STATE_LOCK:
        return _CORRELATIONS.pop(key, "")


def _remember_user_message(session_id: str, user_message: str) -> bool:
    """Return True once per fresh user prompt for the given session."""
    if not user_message:
        return False
    session_key = session_id or "default"
    with _STATE_LOCK:
        last_seen = _LAST_USER_MESSAGE.get(session_key, "")
        is_new = user_message != last_seen
        _LAST_USER_MESSAGE[session_key] = user_message
        if len(_LAST_USER_MESSAGE) > _MAX_TRACKED_STATE:
            # Dict insertion order is stable; drop the oldest session entry.
            oldest = next(iter(_LAST_USER_MESSAGE))
            _LAST_USER_MESSAGE.pop(oldest, None)
        return is_new


def _cache_model(provider: str, model: str) -> None:
    global _LAST_MODEL, _LAST_PROVIDER
    with _STATE_LOCK:
        _LAST_MODEL = model
        _LAST_PROVIDER = provider


def _cached_model_provider() -> tuple[str, str]:
    with _STATE_LOCK:
        return _LAST_MODEL, _LAST_PROVIDER


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
    last_model, last_provider = _cached_model_provider()
    if last_model and "model" not in merged:
        merged["model"] = last_model
    if last_provider and "provider" not in merged:
        merged["provider"] = last_provider
    return merged


def _build_envelope(
    event_type: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
    task_id: str = "",
    correlation_id: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = _config()
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": cfg["source"],
        "agent_id": cfg["source"],
        "session_id": session_id or "default",
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "correlation_id": correlation_id,
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

def on_pre_api_request(
    model: str = "",
    provider: str = "",
    session_id: str = "",
    task_id: str = "",
    user_message: str = "",
    is_first_turn: bool = False,
    **_ignore: Any,
) -> Optional[Dict[str, Any]]:
    """Called by hermes-agent before an LLM API call.

    Emits PreLLMCall to HookBus. If any subscriber returns `deny`, block.

    On the first turn of a session (CLI mode bypasses the gateway hook),
    additionally fire a UserPromptSubmit event so the CRE subscriber gets
    the prompt and returns its KB-augmented preprompt; we then inject it
    into the model context via `{"context": ...}` (Hermes appends plugin
    context to the user message before the LLM call).
    """
    corr = str(uuid.uuid4())
    cache_key = task_id or session_id or "default"
    _put_correlation(cache_key, corr)

    envelope = _build_envelope(
        event_type="PreLLMCall",
        tool_name="llm.api_request",
        tool_input={},
        session_id=session_id,
        task_id=task_id,
        correlation_id=corr,
    )

    verdict = _post_event(envelope)
    cfg = _config()

    if verdict is None:
        if cfg["fail_mode"] == "closed":
            return {
                "action": "block",
                "message": "HookBus unreachable and fail_mode=closed, LLM call denied.",
            }
        return None

    decision = str(verdict.get("decision", "allow")).lower()
    reason = verdict.get("reason", "") or ""

    # CLI compat: gateway runs already fire UserPromptSubmit via
    # pre_gateway_dispatch, but `hermes chat ...` (CLI) does not. We need
    # KB context on every fresh user turn (initial OR mid-session follow-up),
    # not just is_first_turn — Hermes' pre_llm_call fires for every LLM call
    # in a tool-use loop, so we use the user_message-changed signal to fire
    # exactly once per real user prompt.
    sess = session_id or "default"
    is_new_user_turn = _remember_user_message(sess, user_message)

    cre_ctx = ""
    if is_new_user_turn:
        ups = _build_envelope(
            event_type="UserPromptSubmit",
            tool_name="user.prompt",
            tool_input={"prompt": user_message},
            session_id=sess,
            correlation_id=str(uuid.uuid4()),
        )
        ups_verdict = _post_event(ups)
        if ups_verdict and str(ups_verdict.get("decision", "allow")).lower() == "allow":
            cre_ctx = _extract_cre_context(ups_verdict.get("reason", "") or "")

    if decision in ("deny", "ask"):
        response: Dict[str, Any] = {
            "action": "block",
            "message": reason or "Blocked by HookBus subscriber (no reason given).",
        }
        if cre_ctx:
            response["context"] = cre_ctx
        return response
    if decision != "allow":
        logger.warning("hookbus returned unknown decision '%s' for PreLLMCall, defaulting to allow", decision)

    if cre_ctx:
        return {"context": cre_ctx}
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
    reasoning_content: Optional[str] = None,
    assistant_content: Optional[str] = None,
    **_ignore: Any,
) -> None:
    """Called by hermes after each LLM API round-trip.

    Captures exact token usage from hermes' normalised usage dict, emits a
    PostLLMCall event so TokenGuard and other cost-tracking subscribers record
    real spend, and caches the model so subsequent tool events can be tagged
    with the model that selected them.
    """
    current_model = response_model or model or ""
    current_provider = provider or ""
    _cache_model(current_provider, current_model)

    u = usage or {}
    tok_in = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    tok_out = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    total = int(u.get("total_tokens") or (tok_in + tok_out))

    cache_key = task_id or session_id or "default"
    corr = _pop_correlation(cache_key)

    envelope = _build_envelope(
        event_type="PostLLMCall",
        tool_name="llm.api_request",
        tool_input={},
        session_id=session_id,
        task_id=task_id,
        correlation_id=corr,
        extra={
            "model": current_model,
            "provider": current_provider,
            "tokens_input": tok_in,
            "tokens_output": tok_out,
            "total_tokens": total,
            "api_duration_ms": int(api_duration * 1000) if api_duration else 0,
            "tool_calls_emitted": assistant_tool_call_count,
            "assistant_content_chars": assistant_content_chars,
            "reasoning_content": reasoning_content,
            "reasoning_chars": len(reasoning_content or ""),
            "response_content": assistant_content or "",
        },
    )
    _post_event(envelope)
    return None


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
    corr = str(uuid.uuid4())
    if tool_call_id:
        _put_correlation(tool_call_id, corr)

    envelope = _build_envelope(
        event_type="PreToolUse",
        tool_name=tool_name,
        tool_input=args or {},
        session_id=session_id,
        tool_call_id=tool_call_id,
        task_id=task_id,
        correlation_id=corr,
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

    if decision in ("deny", "ask"):
        return {
            "action": "block",
            "message": reason or "Blocked by HookBus subscriber (no reason given).",
        }
    if decision != "allow":
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
    corr = _pop_correlation(tool_call_id) if tool_call_id else ""

    envelope = _build_envelope(
        event_type="PostToolUse",
        tool_name=tool_name,
        tool_input=args or {},
        session_id=session_id,
        tool_call_id=tool_call_id,
        task_id=task_id,
        correlation_id=corr,
        extra={"result_excerpt": str(result)[:500] if result is not None else ""},
    )
    _post_event(envelope)
    return None


def on_pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **_ignore: Any,
) -> Optional[Dict[str, Any]]:
    """Called by hermes-agent gateway when a user message arrives, before auth.

    Emits UserPromptSubmit to HookBus with the user's prompt text + source
    attribution. If any subscriber returns `deny`, drop the message via
    {"action": "skip", "reason": ...}; gateway will not dispatch to the agent.
    """
    if event is None:
        return None

    text = getattr(event, "text", "") or ""
    src = getattr(event, "source", None)
    platform = ""
    chat_id = ""
    user_id = ""
    if src is not None:
        plat = getattr(src, "platform", None)
        platform = getattr(plat, "value", "") or (str(plat) if plat else "")
        chat_id = str(getattr(src, "chat_id", "") or "")
        user_id = str(getattr(src, "user_id", "") or "")
    if text:
        _remember_user_message(chat_id or "default", text)

    envelope = _build_envelope(
        event_type="UserPromptSubmit",
        tool_name="user.prompt",
        tool_input={"prompt": text},
        session_id=chat_id or "default",
        correlation_id=str(uuid.uuid4()),
        extra={
            "platform": platform,
            "chat_id": chat_id,
            "user_id": user_id,
            "prompt_chars": len(text),
        },
    )

    verdict = _post_event(envelope)
    cfg = _config()

    if verdict is None:
        if cfg["fail_mode"] == "closed":
            return {"action": "skip", "reason": "HookBus unreachable and fail_mode=closed"}
        return None

    decision = str(verdict.get("decision", "allow")).lower()
    reason = verdict.get("reason", "") or ""
    if decision in ("deny", "ask"):
        return {"action": "skip", "reason": reason or "Blocked by HookBus subscriber"}
    if decision != "allow":
        logger.warning("hookbus returned unknown decision '%s' for UserPromptSubmit, defaulting to allow", decision)

    # Inject CRE's KB-augmented preprompt into the user message so it reaches
    # the model. CRE puts its context in `reason`; the bus consolidates with
    # `[<subscriber>] ...; [<next>] ...` formatting. We pluck out only the
    # `[cre]` chunk to avoid injecting other subscribers' status messages
    # (DLP clean / KB no matches / orchestrator no conflicts / etc.).
    cre_ctx = _extract_cre_context(reason)
    if cre_ctx:
        return {
            "action": "rewrite",
            "text": cre_ctx + "\n\n" + text,
        }
    return None


def _extract_cre_context(reason: str) -> str:
    """Pull the CRE subscriber's reason chunk out of the bus's consolidated
    reason string. Format: ``[cre] <preprompt>; [kb-injector] ...``."""
    if not reason:
        return ""
    chunks = re.split(r";\s*(?=\[[\w-]+\])", reason)
    for chunk in chunks:
        if chunk.startswith("[cre] "):
            return chunk[6:].strip()
    return ""


# ---------------------------------------------------------------------------
# hermes-agent plugin entry point
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Called by hermes-agent's plugin loader at startup.

    Both the v0.9-era hook names (`pre_api_request`, `post_api_request`)
    and the v0.11+ canonical names (`pre_llm_call`, `post_llm_call`) are
    registered so the same plugin works on either runtime version. The
    runtime invokes whichever names it knows; unknown names are stored
    but never fired, so dual-registration is safe.
    """
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_hook("pre_api_request", on_pre_api_request)        # v0.9
    ctx.register_hook("post_api_request", on_post_api_request)      # v0.9
    ctx.register_hook("pre_llm_call", on_pre_api_request)           # v0.11
    ctx.register_hook("post_llm_call", on_post_api_request)         # v0.11
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    cfg = _config()
    logger.info(
        "hookbus-publisher %s registered: url=%s fail_mode=%s",
        __version__, cfg["url"], cfg["fail_mode"],
    )
