"""Low-cost live progress notices for running BacklinkHub delegations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, Callable, Dict, Optional

from tools.delegation_output_schema import merge_authoritative_tool_facts
from tools.delegation_live_log import get_manifest_checkpoints, get_manifest_tool_facts

DEFAULT_INTERVAL_SECONDS = 300.0
BACKLINK_IDEMPOTENCY_METADATA_KEY = "idempotency_key"


def backlinkhub_event_key(event: Dict[str, Any], *, kind: str) -> str:
    """Return a bounded key for one normalized BacklinkHub business event."""
    counts = {
        name: event.get(name)
        for name in ("published", "pending", "pending_review", "attempted_unconfirmed", "remaining",
                     "failed_retryable", "failed_final", "target_reached", "candidate_bound")
        if event.get(name) is not None
    }
    normalized = {
        "kind": str(kind).strip(),
        "run_id": str(event.get("run_id") or event.get("delegation_id") or "").strip(),
        "site_id": str(event.get("site_id") or "").strip(),
        "status": str(event.get("business_status") or event.get("status") or "").strip().lower(),
        "delegation_status": str(event.get("delegation_status") or "").strip().lower(),
        "stop_reason": str(event.get("stop_reason") or event.get("continuation_stop_kind") or "").strip().lower(),
        "checkpoint": str(event.get("checkpoint") or "").strip().lower(),
        "counts": counts,
    }
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return f"backlinkhub:{kind}:{digest[:32]}"


def configured_interval(config: Any) -> float:
    """Read the operator opt-in interval without affecting normal gateway config."""
    try:
        value = ((config or {}).get("delegation") or {}).get("profile_routes", {}).get("backlinkhub", {}).get(
            "progress_interval_seconds", DEFAULT_INTERVAL_SECONDS
        )
        return max(1.0, float(value))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS


def progress_due(last_sent_at: Optional[float], now: float, interval: float) -> bool:
    """Return whether a live tick may be sent; failed sends keep the old timestamp."""
    return last_sent_at is None or now - last_sent_at >= max(1.0, float(interval))


def build_live_progress(record: Dict[str, Any], *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Read only the detached manifest, producing a validated public snapshot."""
    if record.get("status") not in {"running", "stalling", "stalled"}:
        return None
    if record.get("tool_profile") != "backlinkhub":
        return None
    delegation_id = str(record.get("live_delegation_id") or record.get("delegation_id") or "").strip()
    if not delegation_id:
        return None
    facts = get_manifest_tool_facts(delegation_id)
    payload: Dict[str, Any] = {
        "backlinkhub": True,
        "site_id": record.get("site_id"),
        "run_id": record.get("run_id"),
    }
    merged = merge_authoritative_tool_facts(payload, facts)
    checkpoints = get_manifest_checkpoints(delegation_id)
    checkpoint = next(
        (item for item in reversed(checkpoints) if str(item.get("stage") or "").strip()), None
    )
    if checkpoint:
        merged["checkpoint"] = checkpoint.get("stage")
        merged["checkpoint_at"] = checkpoint.get("at")
    merged.update({
        "delegation_id": record.get("delegation_id"),
        "session_key": record.get("session_key"),
        "parent_session_id": record.get("parent_session_id"),
        "origin_ui_session_id": record.get("origin_ui_session_id"),
        "origin_session_id": record.get("origin_session_id"),
        "started_at": record.get("dispatched_at"),
        "elapsed_seconds": max(0, round((now if now is not None else time.time()) - float(record.get("dispatched_at") or 0), 1)),
    })
    for field in ("platform", "chat_id", "chat_type", "thread_id", "message_id", "scope_id", "user_id", "user_name"):
        value = record.get(field)
        if value is not None and str(value).strip():
            merged[field] = value
    return merged if merged.get("site_id") else None


def format_live_progress(snapshot: Dict[str, Any]) -> str:
    """Render counts from tool facts; ``published`` never becomes ``pending``."""
    site_id = str(snapshot.get("site_id") or "unknown").strip()
    parts = [f"BacklinkHub 站点 {site_id}：运行中"]
    labels = (
        ("已计入", "published"),
        ("待审核", "pending"),
        ("未确认", "attempted_unconfirmed"),
        ("剩余", "remaining"),
    )
    for label, key in labels:
        if snapshot.get(key) is not None:
            parts.append(f"{label} {snapshot[key]}")
    if snapshot.get("checkpoint"):
        parts.append(f"阶段 {snapshot['checkpoint']}")
    if snapshot.get("elapsed_seconds") is not None:
        parts.append(f"已运行 {snapshot['elapsed_seconds']} 秒")
    return "；".join(parts)


async def relay_live_progress(
    snapshot: Dict[str, Any], *, validate_target: Optional[Callable[[dict], Any]] = None,
    build_source: Callable[[dict], Any], resolve_adapter: Callable[[str, Any], Any],
    thread_metadata: Callable[[Any], Optional[Dict[str, Any]]], logger: logging.Logger,
) -> bool:
    """Push one Feishu interim notice and report success for timestamping."""
    from gateway.config import Platform
    from gateway.wake import adapter_supports_push

    event = dict(snapshot)
    # Route keys are mutable across /new and expiry.  An opaque parent identity
    # is therefore mandatory, and the gateway's canonical resolver owns the
    # boundary/compression decision.
    if validate_target is None or not await validate_target(event):
        return False
    source = await asyncio.to_thread(build_source, event)
    if source is None or getattr(source, "platform", None) != Platform.FEISHU:
        return False
    adapter = resolve_adapter("feishu", source)
    if adapter is None or not adapter_supports_push(adapter):
        return False
    metadata = dict(thread_metadata(source) or {})
    metadata["_interim_send"] = True
    metadata[BACKLINK_IDEMPOTENCY_METADATA_KEY] = backlinkhub_event_key(snapshot, kind="live_progress")
    content = format_live_progress(snapshot)
    try:
        result = await adapter.send(source.chat_id, content, metadata=metadata)
    except Exception:
        logger.exception("BacklinkHub live progress notice failed for %s", snapshot.get("site_id"))
        return False
    if getattr(result, "success", True) is False:
        # Feishu rejects stale/fixed thread routing fields with 99992402. Retry
        # the same idempotent notice using the canonical reply anchor; this
        # side-channel must never affect the delegation's business calls.
        error = str(getattr(result, "error", "") or "").strip()
        if re.search(r"(?:^|\D)99992402(?:\D|$)", error) and "thread_id" in metadata:
            fallback_metadata = dict(metadata)
            fallback_metadata.pop("thread_id", None)
            try:
                fallback = await adapter.send(source.chat_id, content, metadata=fallback_metadata)
            except Exception:
                logger.exception("BacklinkHub live progress fallback failed for %s", snapshot.get("site_id"))
                return False
            if getattr(fallback, "success", True) is not False:
                return True
        logger.warning("BacklinkHub live progress notice rejected for %s: %s", snapshot.get("site_id"), error or "unknown error")
        return False
    return True
