#!/usr/bin/env python3
"""
Delegate Tool -- Subagent Architecture

Spawns child AIAgent instances with isolated context, inherited toolsets,
and their own terminal sessions. Supports single-task and batch (parallel)
modes. Top-level model calls run in the background; orchestrator children
wait for their own workers so they can synthesize the results.

Each child gets:
  - A fresh conversation (no parent history)
  - Its own task_id (own terminal session, file ops cache)
  - The parent's toolsets, optionally narrowed by an operator-defined profile,
    with child-only blocked tools stripped
  - A focused system prompt built from the delegated goal + context

The parent's context only sees the delegation call and the summary result,
never the child's intermediate tool calls or reasoning.
Spawns child AIAgent instances with a fresh conversation, their own task_id
(terminal session, file-ops cache), the parent's toolsets minus child-blocked
tools, and a focused system prompt built from goal + context. Single-task and
batch (parallel) modes; top-level model calls run in the background while
orchestrator children wait for their own workers. The parent only ever sees
the delegation call and the summary result, never the child's intermediate
tool calls or reasoning.
"""

import logging
import re

logger = logging.getLogger(__name__)
DEFAULT_CHILD_TIMEOUT = None
import os
import shutil
import subprocess
import threading
import time
import weakref
from typing import Any, Dict, List, Optional

from toolsets import TOOLSETS, validate_toolset
from agent.interrupt_compat import request_hard_interrupt
from tools.terminal_tool import set_approval_callback as _set_subagent_approval_cb  # noqa: F401  (used via _ChildRun.await_child)
from utils import is_truthy_value

logger = logging.getLogger(__name__)

# The delegate_tool_* siblings hold the pieces split out of this module; every name callers or patching tests reach as
# ``tools.delegate_tool.<name>`` is re-imported here. Mutable flag globals live only in their owning module.
from tools.delegate_tool_child_run import (  # noqa: F401
    _ChildRun, _attach_child, _build_result_entry, _dump_subagent_timeout_diagnostic, _fabricated_entry,
    _lease_child_credential, _merge_late_steer, _register_child, _start_heartbeat, _validate_child_output_schema,
)
from tools.delegate_tool_config import (  # noqa: F401
    _DEFAULT_MAX_CONCURRENT_CHILDREN, _get_child_timeout, _get_max_async_children, _get_max_concurrent_children,
    _get_max_spawn_depth, _get_orchestrator_enabled, _get_subagent_approval_callback, _get_worktree_isolation,
    _inherit_parent_capabilities, _load_config, _merge_request_overrides, _resolve_child_credential_pool,
    _resolve_child_runtime, _resolve_delegation_credentials,
    _subagent_auto_approve, _subagent_auto_deny,
)
from tools.delegate_tool_dispatch import (
    _Batch,
    _announce_batch,
    _capture_origin,
    _reject_unstarted_batch,
    _run_batch,
)
from tools.delegate_tool_progress import (  # noqa: F401
    DelegateEvent, SUBAGENT_FAILURE_STATUSES, _batch_prefix, _build_child_progress_callback,
    _build_child_system_prompt, _clean_error_text, _emit_parent_console, _quiet, _resolve_workspace_hint,
    _safe_progress, format_batch_tag, format_subagent_failure_line,
)
from tools.delegate_tool_registry import (  # noqa: F401
    _CONTROL_ACTIONS, _active_subagents, _active_subagents_lock, _capture_gateway_steer_authority,
    _handle_control_action, _is_descendant_of, _owns_subagent_record, _register_subagent, _unregister_subagent,
    get_subagent_attribution, interrupt_subagent, is_spawn_paused, list_active_subagents, set_spawn_paused,
    steer_subagent,
)
from tools.delegate_tool_tasks import _coerce_task_schemas, _normalize_task_list
from tools.delegate_tool_toolsets import (  # noqa: F401
    DELEGATE_BLOCKED_TOOLS, _expand_parent_toolsets, _resolve_child_toolsets, _strip_blocked_tools,
)
from tools.delegate_tool_results import (  # noqa: F401
    _apply_summary_budget, _build_child_preserving_parent_tools, _run_child_lifecycle, _summarize_tool_arguments,
)

_ROLES = frozenset({"leaf", "orchestrator"})

# A schema-constrained child may return a long JSON summary.  The host can
# shorten that summary before an asynchronous completion event is built, so
# keep only the small fields needed to resume a detached task at the result
# boundary.  This is deliberately private metadata; it is removed before a
# result is returned to a model or written to the durable completion record.
_CONTINUATION_RESULT_FIELDS = (
    "site_id",
    "run_id",
    "target",
    "published",
    "pending",
    "attempted_unconfirmed",
    "failed_retryable",
    "failed_final",
    "remaining",
    "queue_exhausted",
    "target_reached",
    "stop_reason",
    "reported_stop_reason",
    "ego_task_space_id",
    "ego_cleanup",
    "segment_iteration_boundary",
    "candidate_bound",
    "candidate_external_side_effect",
)


def _validated_completion_metadata(
    summary: Any, *, schema_valid: Any, exit_reason: str
) -> Dict[str, Any]:
    """Extract compact continuation fields before summary processing.

    ``schema_valid`` is checked by the caller, but keeping the guard here
    makes this helper safe to reuse from tests and future result paths.  The
    output contract validator already accepted the text; this second parse is
    intentionally bounded to the known continuation fields only.
    """
    if schema_valid is not True:
        return {}
    metadata: Dict[str, Any] = {
        "schema_valid": True,
        "exit_reason": exit_reason,
        "truncated": exit_reason == "max_iterations",
    }
    try:
        from tools.delegation_output_schema import (
            extract_json_candidate,
            normalize_completion_exit_reason,
            normalize_completion_payload,
        )

        payload = json.loads(extract_json_candidate(summary or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return metadata
    if not isinstance(payload, dict):
        return metadata
    nested = payload.get("summary")
    if (
        isinstance(nested, dict)
        and any(key in nested for key in _CONTINUATION_RESULT_FIELDS)
    ):
        # Unwrap one result envelope only when it contains known continuation
        # facts. A multi-site aggregate must never become guessed site/run
        # metadata.
        payload = nested
    payload = normalize_completion_payload(payload)
    metadata["exit_reason"] = normalize_completion_exit_reason(
        payload,
        schema_valid=schema_valid,
        exit_reason=exit_reason,
    )
    metadata["truncated"] = metadata["exit_reason"] == "max_iterations"
    for key in _CONTINUATION_RESULT_FIELDS:
        if key in payload:
            metadata[key] = payload[key]
    return metadata


def _strip_internal_completion_metadata(payload: Any) -> Any:
    """Return a wire-safe copy without host-only continuation metadata."""
    if not isinstance(payload, dict):
        return payload
    cleaned = dict(payload)
    cleaned.pop("_completion_metadata", None)
    results = cleaned.get("results")
    if isinstance(results, list):
        cleaned["results"] = [
            _strip_internal_completion_metadata(item) for item in results
        ]
    return cleaned


def _cleanup_empty_agent_ego_space(space_id: Any) -> Dict[str, Any]:
    """Close one proven-empty agent-owned Ego space after transport failure."""

    if not isinstance(space_id, int) or isinstance(space_id, bool) or space_id <= 0:
        return {"closed": False, "reason": "invalid_space_id"}
    ego_browser = shutil.which("ego-browser")
    if not ego_browser:
        fallback = os.path.expanduser("~/.local/bin/ego-browser")
        ego_browser = fallback if os.path.isfile(fallback) else None
    if not ego_browser:
        return {"closed": False, "reason": "ego_browser_unavailable"}

    script = f"""
const id = {space_id}
const spaces = await listTaskSpaces()
const space = spaces.find(item => Number(item.id) === id)
if (!space) {{
  cliLog(JSON.stringify({{closed: false, reason: 'space_not_found'}}))
}} else if (space.ownership !== 'agent') {{
  cliLog(JSON.stringify({{closed: false, reason: 'ownership_not_agent', ownership: space.ownership}}))
}} else {{
  await useOrCreateTaskSpace(id)
  const tabs = await listTabs()
  const onlyBlank = tabs.length > 0 && tabs.every(tab => String(tab.url || '') === 'about:blank')
  if (!onlyBlank) {{
    cliLog(JSON.stringify({{closed: false, reason: 'nonblank_or_unknown_tabs', tabCount: tabs.length}}))
  }} else {{
    const result = await completeTaskSpace(id, {{keep: false}})
    cliLog(JSON.stringify({{closed: result?.done === true, reason: result?.done === true ? 'closed' : 'cleanup_not_confirmed'}}))
  }}
}}
""".strip()
    try:
        completed = subprocess.run(
            [ego_browser, "nodejs"],
            input=script,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"closed": False, "reason": "cleanup_command_failed", "error": str(exc)}
    if completed.returncode != 0:
        return {
            "closed": False,
            "reason": "cleanup_command_failed",
            "error": (completed.stderr or completed.stdout or "")[-500:],
        }
    for line in reversed((completed.stdout or "").splitlines()):
        try:
            result = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(result, dict) and isinstance(result.get("closed"), bool):
            return result
    return {"closed": False, "reason": "cleanup_result_unreadable"}


# ---------------------------------------------------------------------------
# Subagent approval callbacks
# ---------------------------------------------------------------------------
# Subagents run inside a ThreadPoolExecutor worker. The CLI's interactive
# approval callback is stored in tools/terminal_tool.py's threading.local(),
# so worker threads do NOT inherit it. Without a callback,
# prompt_dangerous_approval() falls back to input() from the worker thread,
# which deadlocks against the parent's prompt_toolkit TUI that owns stdin.
#
# Fix: install a non-interactive callback into every subagent worker thread
# via ThreadPoolExecutor(initializer=_set_subagent_approval_cb, initargs=(cb,)).
# The callback is chosen by the `delegation.subagent_auto_approve` config:
#   false (default) → _subagent_auto_deny (safe; matches leaf tool blocklist)
#   true            → _subagent_auto_approve (opt-in YOLO for cron/batch)
# Both emit a logger.warning for audit; gateway sessions are unaffected
# because they resolve approvals via tools/approval.py's per-session queue,
# not through these TLS callbacks.





# NOTE: nested delegation is granted by role='orchestrator' (which re-adds the
# "delegation" toolset in _build_child_agent), NOT by the model naming raw
# toolsets. Subagents inherit the parent's tools unless the model selects an
# operator-defined named profile, which can only narrow that inherited set.

_DEFAULT_MAX_CONCURRENT_CHILDREN = 10
# One-shot guard: the high-concurrency cost advisory is emitted at most once
# per process. _get_max_concurrent_children() runs on every get_definitions()
# schema rebuild (via _build_top_level_description / _build_tasks_param_description),
# so without this flag a config of max_concurrent_children>10 spams the log on
# every turn / agent spawn even when delegate_task is never called.
_HIGH_CONCURRENCY_WARNED = False
MAX_DEPTH = 1  # flat by default: parent (0) -> child (1); grandchild rejected unless max_spawn_depth raised.
# Configurable depth cap consulted by _get_max_spawn_depth; MAX_DEPTH
# stays as the default fallback and is still the symbol tests import.
_MIN_SPAWN_DEPTH = 1
# No upper ceiling on spawn depth — like max_concurrent_children, depth has a
# floor of 1 and no ceiling. Deeper trees multiply API cost, so the default
# stays flat (MAX_DEPTH = 1); raising the config knob is an explicit opt-in.


# ---------------------------------------------------------------------------
# Runtime state: pause flag + active subagent registry
#
# Consumed by the TUI observability layer (overlay/control surface) and the
# gateway RPCs `delegation.pause`, `delegation.status`, `subagent.interrupt`.
# Kept module-level so they span every delegate_task invocation in the
# process, including nested orchestrator -> worker chains.
# ---------------------------------------------------------------------------

_spawn_pause_lock = threading.Lock()
_spawn_paused: bool = False

_active_subagents_lock = threading.Lock()
# subagent_id -> mutable record tracking the live child agent.  Stays only
# for the lifetime of the run; _run_single_child is the owner.
_active_subagents: Dict[str, Dict[str, Any]] = {}

# subagent_id -> {goal, delegation_id, parent_session_id} retained AFTER the
# child finishes (bounded FIFO). Child-started background processes routinely
# outlive the child itself (its npm ci with notify_on_complete=true finishes
# after the child's summary was delivered); their completion notifications
# reach the parent conversation via the shared completion_queue and need
# delegation attribution even though the live registry entry is gone.
_RECENT_SUBAGENTS_CAP = 200
_recent_subagents: Dict[str, Dict[str, Any]] = {}










def _retain_recent_subagent(record: Dict[str, Any]) -> None:
    """Keep a bounded attribution stub after a child finishes (lock held)."""
    sid = record.get("subagent_id")
    if not sid:
        return
    _recent_subagents[sid] = {
        "goal": record.get("goal"),
        "delegation_id": record.get("delegation_id"),
        "owner_agent_session_id": record.get("owner_agent_session_id"),
    }
    while len(_recent_subagents) > _RECENT_SUBAGENTS_CAP:
        _recent_subagents.pop(next(iter(_recent_subagents)), None)




def _close_subagent_steering(subagent_id: str, agent: Any) -> Optional[str]:
    """Atomically close steer acceptance and drain its final durable artifact.

    ``steer_subagent`` holds the same registry lock through ``agent.steer``.
    Therefore either acceptance wins and this drain sees its exact text, or
    closure wins and the caller is rejected. Exact agent identity prevents a
    finishing child with a recycled public id from closing its replacement.
    """
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if record is None or record.get("agent") is not agent:
            return None
        record["accepting_steer"] = False
        drain = getattr(agent, "_drain_pending_steer", None)
        if not callable(drain):
            return None
        try:
            pending = drain()
        except Exception as exc:
            logger.debug("final steer drain for %s failed: %s", subagent_id, exc)
            return None
        return pending if isinstance(pending, str) and pending.strip() else None












# Model-facing control actions accepted by delegate_task(action=...).
# "spawn" (or omitted) keeps the historical spawn semantics.
_CONTROL_ACTIONS = frozenset({"list", "steer", "stop"})


def _resolve_session_lineage(session_id: Optional[str], parent_agent: Any) -> str:
    """Resolve a session id to the tip of its compression lineage.

    Best-effort: uses the parent's live SessionDB handle when present so a
    delegation dispatched before a compression rotation still matches the
    rotated parent. Returns the input unchanged when resolution fails.
    """
    sid = str(session_id or "")
    if not sid:
        return ""
    db = getattr(parent_agent, "_session_db", None)
    if db is None:
        return sid
    try:
        resolved = db.resolve_resume_session_id(sid)
        return str(resolved) if resolved else sid
    except Exception:
        return sid






def _extract_output_tail(
    result: Dict[str, Any],
    *,
    max_entries: int = 12,
    max_chars: int = 8000,
) -> List[Dict[str, Any]]:
    """Pull the last N tool-call results from a child's conversation.

    Powers the overlay's "Output" section — the cc-swarm-parity feature.
    We reuse the same messages list the trajectory saver walks, taking
    only the tail to keep event payloads small.  Each entry is
    ``{tool, preview, is_error}``.
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return []

    # Walk in reverse to build a tail; stop when we have enough.
    tail: List[Dict[str, Any]] = []
    pending_call_by_id: Dict[str, str] = {}

    # First pass (forward): build tool_call_id -> tool_name map
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                if tc_id:
                    pending_call_by_id[tc_id] = str(fn.get("name") or "tool")

    # Second pass (reverse): pick tool results, newest first
    for msg in reversed(messages):
        if len(tail) >= max_entries:
            break
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        # Flatten content-block lists/dicts to text so the overlay shows real
        # output (not a "[{'type': 'text'...}]" blob) and error detection can
        # see markers buried inside content blocks. Crude str() here would
        # mislabel a block-wrapped "Error: ..." result as is_error=False.
        content = _stringify_tool_content(msg.get("content") or "")
        is_error = _looks_like_error_output(content)
        tool_name = pending_call_by_id.get(msg.get("tool_call_id") or "", "tool")
        # Preserve line structure so the overlay's wrapped scroll region can
        # show real output rather than a whitespace-collapsed blob. We still
        # cap the payload size to keep events bounded.
        preview = content[:max_chars]
        tail.append({"tool": tool_name, "preview": preview, "is_error": is_error})

    tail.reverse()  # restore chronological order for display
    return tail


def _stringify_tool_content(content: Any) -> str:
    """Return a stable text representation for tool-result content.

    Most providers store tool results as strings, but some OpenAI-compatible
    paths can return content-block lists. Delegate observability must never
    crash while summarising a child run just because the transport used blocks.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


_TOOL_INPUT_TARGET_KEYS = frozenset({
    "cwd",
    "destination_path",
    "directory",
    "dst",
    "endpoint",
    "file_path",
    "new_path",
    "old_path",
    "path",
    "source_path",
    "src",
    "target_path",
    "url",
    "urls",
})
_TOOL_INPUT_URL_KEYS = frozenset({"endpoint", "url", "urls"})


def _sanitize_tool_target(key: str, value: Any) -> Any:
    """Keep bounded side-effect targets while dropping URL secrets."""
    if isinstance(value, list):
        cleaned = [
            item for item in (_sanitize_tool_target(key, item) for item in value[:16])
            if item is not None
        ]
        return cleaned or None
    if not isinstance(value, str) or not value:
        return None
    bounded = value[:1024]
    if key in _TOOL_INPUT_URL_KEYS:
        try:
            parsed = urlsplit(bounded)
            if parsed.scheme and parsed.netloc:
                hostname = parsed.hostname
                if not hostname:
                    return None
                # ``SplitResult.netloc`` includes ``user:password@``. Rebuild
                # the authority from parsed host/port so hook-visible history
                # cannot carry URL credentials. Bracket IPv6 literals before
                # appending a validated port.
                host = f"[{hostname}]" if ":" in hostname else hostname
                port = parsed.port
                netloc = f"{host}:{port}" if port is not None else host
                return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except ValueError:
            return None
    return bounded




def _sanitize_tool_input_summary(summary: Any) -> Dict[str, Any]:
    if not isinstance(summary, dict):
        return {"argument_keys": [], "targets": {}}
    keys = summary.get("argument_keys")
    safe_keys = (
        [str(key)[:128] for key in keys[:64]]
        if isinstance(keys, list)
        else []
    )
    targets = summary.get("targets")
    safe_targets: Dict[str, Any] = {}
    if isinstance(targets, dict):
        for raw_key, value in targets.items():
            key = str(raw_key).lower()
            if key not in _TOOL_INPUT_TARGET_KEYS:
                continue
            cleaned = _sanitize_tool_target(key, value)
            if cleaned is not None:
                safe_targets[key] = cleaned
    return {"argument_keys": safe_keys, "targets": safe_targets}


def _subagent_stop_tool_call_history(tool_trace: Any) -> List[Dict[str, Any]]:
    """Build a detached, metadata-only tool history for lifecycle hooks."""
    if not isinstance(tool_trace, list):
        return []

    history: List[Dict[str, Any]] = []
    for item in tool_trace:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool") or "unknown")[:256]
        status = str(item.get("status") or "unknown").lower()
        if status not in {"ok", "error"}:
            status = "unknown"

        def _byte_count(key: str) -> int:
            value = item.get(key, 0)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return 0
            return max(0, int(value))

        history.append({
            "tool_name": tool_name,
            "tool_input": _sanitize_tool_input_summary(item.get("input_summary")),
            "input_bytes": _byte_count("args_bytes"),
            "output_bytes": _byte_count("result_bytes"),
            "status": status,
        })
    return history


def _looks_like_error_output(content: Any) -> bool:
    """Conservative stderr/error detector for tool-result previews.

    The old heuristic flagged any preview containing the substring "error",
    which painted perfectly normal terminal/json output red.  We now only
    mark output as an error when there is stronger evidence:
      - structured JSON with an ``error`` key
      - structured JSON with ``status`` of error/failed
      - first line starts with a classic error marker
    """
    content = _stringify_tool_content(content)
    if not content:
        return False

    head = content.lstrip()
    if head.startswith("{") or head.startswith("["):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    return True
                status = str(parsed.get("status") or "").strip().lower()
                if status in {"error", "failed", "failure", "timeout"}:
                    return True
        except Exception:
            pass

    first = content.splitlines()[0].strip().lower() if content.splitlines() else ""
    return (
        first.startswith("error:")
        or first.startswith("failed:")
        or first.startswith("traceback ")
        or first.startswith("exception:")
    )


# Nested delegation is granted by depth/role in _build_child_agent, never by the
# model naming toolsets (there is no model-facing toolsets argument).
def _normalize_role(r: Optional[str]) -> str:
    """'leaf' | 'orchestrator'; None/empty/unknown -> 'leaf' (unknown warns)."""
    r_norm = str(r).strip().lower() if r else "leaf"
    if r_norm not in _ROLES:
        logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
        return "leaf"
    r_norm = str(r).strip().lower()
    if r_norm in {"leaf", "orchestrator"}:
        return r_norm
    logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
    return "leaf"






_LEGACY_MAX_ASYNC_WARNED = False










def _get_inherit_mcp_toolsets() -> bool:
    """Whether narrowed child toolsets should keep the parent's MCP toolsets."""
    cfg = _load_config()
    return is_truthy_value(cfg.get("inherit_mcp_toolsets"), default=True)


def _configured_tool_profile_names(cfg: Optional[dict] = None) -> List[str]:
    """Return operator-defined delegation tool profile names."""
    config = cfg if isinstance(cfg, dict) else _load_config()
    profiles = config.get("tool_profiles")
    if not isinstance(profiles, dict):
        return []
    return sorted(
        {
            str(name).strip()
            for name in profiles
            if str(name).strip()
        }
    )


def _resolve_tool_profile(
    profile_name: Optional[str], cfg: Optional[dict] = None
) -> Optional[List[str]]:
    """Resolve a named, operator-controlled child toolset profile.

    The model may select only a configured profile name. It never supplies raw
    toolsets, and ``_build_child_agent`` still intersects this list with the
    parent's effective toolsets before constructing the child.
    """
    if profile_name is None or not str(profile_name).strip():
        return None

    name = str(profile_name).strip()
    config = cfg if isinstance(cfg, dict) else _load_config()
    profiles = config.get("tool_profiles")
    available = _configured_tool_profile_names(config)
    if not isinstance(profiles, dict) or name not in profiles:
        available_text = ", ".join(available) if available else "none"
        raise ValueError(
            f"Unknown delegation tool_profile '{name}'. "
            f"Configured profiles: {available_text}."
        )

    raw_toolsets = profiles[name]
    if not isinstance(raw_toolsets, (list, tuple)):
        raise ValueError(
            f"delegation.tool_profiles.{name} must be a YAML list of toolset names."
        )
    toolsets = list(
        dict.fromkeys(
            str(toolset).strip()
            for toolset in raw_toolsets
            if str(toolset).strip()
        )
    )
    if not toolsets:
        raise ValueError(
            f"delegation.tool_profiles.{name} must contain at least one toolset."
        )
    unknown = [toolset for toolset in toolsets if not validate_toolset(toolset)]
    if unknown:
        raise ValueError(
            f"delegation.tool_profiles.{name} contains unknown toolsets: "
            + ", ".join(unknown)
        )
    return toolsets


def _is_mcp_toolset_name(name: str) -> bool:
    """Return True for canonical MCP toolsets and their registered aliases."""
    if not name:
        return False
    if str(name).startswith("mcp-"):
        return True
    try:
        from tools.registry import registry

        target = registry.get_toolset_alias_target(str(name))
    except Exception:
        target = None
    return bool(target and str(target).startswith("mcp-"))




def _preserve_parent_mcp_toolsets(
    child_toolsets: List[str], parent_toolsets: set[str]
) -> List[str]:
    """Append any parent MCP toolsets that are missing from a narrowed child."""
    preserved = list(child_toolsets)
    for toolset_name in sorted(parent_toolsets):
        if _is_mcp_toolset_name(toolset_name) and toolset_name not in preserved:
            preserved.append(toolset_name)
    return preserved

    return r_norm

DEFAULT_MAX_ITERATIONS = 250
_HEARTBEAT_INTERVAL = 30  # seconds between parent activity heartbeats during delegation
# Stale-heartbeat thresholds (cycles of _HEARTBEAT_INTERVAL with no progress). Progress = iteration, current_tool OR
# last_activity_ts advancing; an in-flight model wait refreshes last_activity_ts, so slow models are not "idle". Idle
# stays tight so a truly wedged child doesn't mask the gateway timeout; in-tool is much higher so legitimately long
# tools can finish.
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 450s idle between turns → stale
_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 1200s stuck on same tool → stale

def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _open_child_session_db(parent_agent) -> Any:
    """DEDICATED SessionDB handle for the child, or None: the parent's handle can be closed by its own lifecycle while
    a background child still flushes (transcript silently dropped). It MUST open the same db FILE as the parent's
    handle (non-launch profiles), else lineage / session_search break; released by the child's close() via
    _owns_session_db."""
    # Each child gets a DEDICATED SessionDB connection instead of the parent's live object. The parent's
    # handle is owned by the parent's lifecycle (cron run_job's finally block, gateway session end, /new)
    # and can be closed while a fire-and-forget background child is still flushing on a daemon thread —
    # every subsequent flush then hits the closed handle and the child's transcript is silently dropped
    # (#81267). It MUST point at the same database FILE as the parent's handle: parents can hold non-default
    # per-profile handles (tui_gateway opens SessionDB(db_path=<profile>/ state.db) for non-launch
    # profiles), and a bare SessionDB() would write the child's transcript into the launch profile's db,
    # breaking parent_session_id lineage and session_search. AsyncSessionDB wrappers (gateway) forward
    # .db_path via __getattr__, so this works through them.
    parent_session_db = getattr(parent_agent, "_session_db", None)
    if parent_session_db is None:
        return None
    with _quiet("subagent: failed to open dedicated SessionDB; child persistence disabled", exc_info=True):
        from hermes_state_registry import acquire
        _parent_db_path = getattr(parent_session_db, "db_path", None)
        child_db = acquire(_parent_db_path) if _parent_db_path is not None else acquire()
        if child_db is not None and _parent_db_path is not None:
            child_db.db_path = _parent_db_path
        return child_db
    return None

def _apply_child_cache_ttl(child) -> None:
    """A delegated child never uses the 1h cache tier. The tier is priced for a person who steps
    away between turns (2x write vs 1.25x for 5m, #14971); a subagent calls every few seconds for
    minutes and is gone, so it pays the 2x on every tool result and never collects the retention.
    Caching itself stays exactly as configured (disabled stays disabled)."""
    if getattr(child, "_cache_ttl", None) == "1h":
        child._cache_ttl = "5m"

_CHILD_CAP_MIN = 16_000  # below this a child compresses on every call; treat as a config error


def _child_compression_cap_tokens(raw) -> "int | None":
    """Validated ``delegation.compression_threshold_tokens``: an int >= 16000, or None for "no cap".

    Unset / ``0`` / ``false`` / ``null`` mean no subagent-specific cap: the child compacts at the
    same ratio trigger as everyone else (0.50 x window). A bool ``true`` (YAML) would coerce to 1
    and make every call compress; a string like ``"200k"`` would silently read as no cap. Both are
    config errors: warn and treat as unset so a typo never changes compaction behaviour."""
    if raw is None or raw is False or raw == 0:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) < _CHILD_CAP_MIN:
        logger.warning(
            "delegation.compression_threshold_tokens=%r is not a token count >= %d; ignoring it "
            "(children keep the ratio trigger).", raw, _CHILD_CAP_MIN,
        )
        return None
    return int(raw)


def _apply_child_compression_cap(child, delegation_cfg: dict) -> None:
    """Optional absolute cap on the child's compaction trigger, ``delegation.compression_threshold_tokens``
    (lower of it and any global ``compression.threshold_tokens``). Off by default: a 1M-window child
    compacts at 500K like its parent. The compressor applies the cap on first window resolution, which
    happens after construction, so setting it here is exactly equivalent to config."""
    from agent.context_compressor import ContextCompressor

    cc = getattr(child, "context_compressor", None)
    if not isinstance(cc, ContextCompressor):
        return
    cap = _child_compression_cap_tokens((delegation_cfg or {}).get("compression_threshold_tokens"))
    if cap is None:
        return
    existing = cc.threshold_tokens_cap
    cc.threshold_tokens_cap = min(cap, existing) if isinstance(existing, int) and existing > 0 else cap
    if cc._threshold_tokens is not None:  # already resolved: re-clamp now
        cc._apply_threshold_tokens_cap()


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    task_count: int,
    parent_agent,
    # Credential overrides from delegation config
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    override_request_overrides: Optional[Dict[str, Any]] = None,

    # ACP transport overrides from trusted delegation config.
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
    # Configuration block that owns the selected provider/model route. Internal
    # callers such as /review pass auxiliary.review here so fallback policy is
    # not accidentally read from the general delegation block.
    routing_cfg: Optional[Dict[str, Any]] = None,
    # Legacy; accepted for wire compat but ignored (capability is depth-derived).
    role: str = "leaf",
):
    """Build (don't run) a child AIAgent on the main thread. override_* (from delegation config) replace parent
    inheritance so children can run on a different provider:model pair."""
    import uuid as _uuid
    from run_agent import AIAgent
    from agent.delegation_context import delegated_child_context
    # Role is depth-derived: a child may delegate iff the kill switch is on and
    # depth budget remains below max_spawn_depth. The `role` arg is ignored.
    child_depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    max_spawn = _get_max_spawn_depth()
    effective_role = "orchestrator" if _get_orchestrator_enabled() and child_depth < max_spawn else "leaf"

    # One subagent_id shared by the progress callback, spawn_requested event and
    # the live registry; parent_id is set when THIS parent is itself a subagent.
    subagent_id = f"sa-{task_index}-{_uuid.uuid4().hex[:8]}"
    parent_subagent_id = getattr(parent_agent, "_subagent_id", None)

    # General delegation behavior (reasoning, compression, capabilities) stays
    # global. Only fallback policy follows the owner of a per-call route such
    # as auxiliary.review.
    delegation_cfg = _load_config()
    child_toolsets, child_disabled_toolsets = _resolve_child_toolsets(parent_agent, toolsets, effective_role)
    child_prompt = _build_child_system_prompt(
        goal, context, workspace_path=_resolve_workspace_hint(parent_agent), role=effective_role,
        max_spawn_depth=max_spawn, child_depth=child_depth,
    )
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # Shared ref: session_id once the child exists, delegation_id once
    # delegate_task stamps it — both ride on every relayed event.
    child_session_ref: Dict[str, Any] = {}
    child_progress_cb = _build_child_progress_callback(
        task_index, goal, parent_agent, task_count, subagent_id=subagent_id, parent_id=parent_subagent_id,
        depth=max(0, child_depth - 1),  # 0 = first-level child for the UI
        model=model or getattr(parent_agent, "model", None), toolsets=child_toolsets, session_ref=child_session_ref,
    )
    rt = _resolve_child_runtime(
        parent_agent, delegation_cfg, parent_api_key, model=model, override_provider=override_provider,
        override_base_url=override_base_url, override_api_key=override_api_key, override_api_mode=override_api_mode,
        override_acp_command=override_acp_command,
        override_acp_args=override_acp_args,
        routing_cfg=routing_cfg,
    )
    if override_request_overrides is not None:
        # honored whenever set, incl. the inherit branch where
        # _resolve_delegation_credentials already merged OVER the parent's
        request_overrides = dict(override_request_overrides)
    else:
        request_overrides = {} if override_provider else dict(getattr(parent_agent, "request_overrides", {}) or {})
    parent_sid = getattr(parent_agent, "session_id", None)
    child_session_db = _open_child_session_db(parent_agent)
    with delegated_child_context():
        try:
            child = AIAgent(
                **rt, max_iterations=max_iterations, prefill_messages=getattr(parent_agent, "prefill_messages", None),
                enabled_toolsets=child_toolsets, disabled_toolsets=child_disabled_toolsets, quiet_mode=True,
                ephemeral_system_prompt=child_prompt, log_prefix=f"[subagent-{task_index}]", platform="subagent",
                skip_context_files=True, skip_memory=True, clarify_callback=None,
                thinking_callback=(
                    (lambda text: _safe_progress(child_progress_cb, "_thinking", text) if text else None)
                    if child_progress_cb else None
                ),
                session_db=child_session_db, parent_session_id=parent_sid, request_overrides=request_overrides,
                tool_progress_callback=child_progress_cb,
                iteration_budget=None,  # fresh budget per subagent
            )
        except BaseException:
            # No child close() will ever run: release the dedicated handle here.
            if child_session_db is not None:
                with _quiet(None):
                    from hermes_state_registry import release_or_close
                    release_or_close(child_session_db)
            raise
    child._print_fn = getattr(parent_agent, "_print_fn", None)
    _apply_child_cache_ttl(child)
    if child_session_db is not None:
        child._owns_session_db = True  # released by the child's close(), never by the parent
    # Ownership transfer for the dedicated handle: the child's close() must release it (nothing else holds a
    # reference), and no parent teardown can close it out from under a background child (#81267).
    child_session_ref["session_id"] = getattr(child, "session_id", "") or ""
    child._progress_identity_ref = child_session_ref
    child._delegate_depth, child._delegate_role = child_depth, effective_role  # post-degrade role
    child._subagent_id, child._parent_subagent_id = subagent_id, parent_subagent_id
    _apply_child_compression_cap(child, delegation_cfg)
    # Ownership chain for action=list/steer/stop; weakref so a finished parent
    # can be collected while a detached child record lingers in the registry.
    try:
        child._delegate_parent_ref = weakref.ref(parent_agent)
    except TypeError:
        child._delegate_parent_ref = None  # non-weakref-able test doubles
    # Sidebar marker: subagent sessions stay out of session pickers even when a
    # parent delete orphans them (mirrors /branch's ``_branched_from``).
    if parent_sid and getattr(child, "_session_init_model_config", None) is not None:
        child._session_init_model_config["_delegate_from"] = parent_sid
    # Shared pool lets children rotate credentials on rate limits.
    child_pool = _resolve_child_credential_pool(rt["provider"], parent_agent, rt["base_url"])
    if child_pool is not None:
        child._credential_pool = child_pool

    _attach_child(parent_agent, child)  # interrupt propagation
    # spawn_requested now — the child may queue for seconds when the pool is
    # saturated — then the subagent_start lifecycle hook.
    _safe_progress(child_progress_cb, "subagent.spawn_requested", preview=goal)
    with _quiet("subagent_start hook invocation failed", exc_info=True):
        from hermes_cli.lifecycle import invoke_hook as _invoke_hook
        _invoke_hook(
            "subagent_start", parent_session_id=parent_sid,
            parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "", parent_subagent_id=parent_subagent_id,
            child_session_id=getattr(child, "session_id", None), child_subagent_id=subagent_id,
            child_role=effective_role, child_goal=goal,
        )
    return child



_PARENT_FINALIZATION_LOCK_GUARD = threading.Lock()
_PARENT_FINALIZATION_FALLBACK_LOCK = threading.RLock()
_CHILD_CONSTRUCTION_LOCK = threading.RLock()




def _parent_finalization_lock(parent_agent) -> threading.RLock:
    """Return the per-parent lock that serializes lifecycle side effects."""
    if parent_agent is None:
        return _PARENT_FINALIZATION_FALLBACK_LOCK
    lock = getattr(parent_agent, "_subagent_finalization_lock", None)
    if lock is not None:
        return lock
    with _PARENT_FINALIZATION_LOCK_GUARD:
        lock = getattr(parent_agent, "_subagent_finalization_lock", None)
        if lock is None:
            lock = threading.RLock()
            try:
                setattr(parent_agent, "_subagent_finalization_lock", lock)
            except Exception:
                return _PARENT_FINALIZATION_FALLBACK_LOCK
    return lock


def _finalize_child_results(
    results: List[Dict[str, Any]],
    task_list: List[Dict[str, Any]],
    children: List[tuple[int, Dict[str, Any], Any]],
    parent_agent,
) -> List[Dict[str, Any]]:
    """Apply host-owned summary, memory, hook, and cost contracts once.

    Return the private continuation metadata captured before summary
    truncation.  Callers carry it only until the async event is built, then
    remove it from all model- and persistence-facing payloads.
    """
    with _parent_finalization_lock(parent_agent):
        completion_metadata: List[Dict[str, Any]] = []
        for entry in results:
            raw_metadata = entry.pop("_completion_metadata", None)
            completion_metadata.append(
                dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            )
        _apply_summary_budget(results, parent_agent)
        child_by_index = {index: child for index, _task, child in children}

        if parent_agent and getattr(parent_agent, "_memory_manager", None):
            for entry in results:
                try:
                    task_index = entry.get("task_index", -1)
                    task_goal = (
                        task_list[task_index]["goal"]
                        if isinstance(task_index, int)
                        and 0 <= task_index < len(task_list)
                        else ""
                    )
                    child = child_by_index.get(task_index)
                    parent_agent._memory_manager.on_delegation(
                        task=task_goal,
                        result=entry.get("summary", "") or "",
                        child_session_id=getattr(child, "session_id", ""),
                    )
                except Exception:
                    pass

        parent_session_id = getattr(parent_agent, "session_id", None)
        try:
            from hermes_cli.plugins import invoke_hook as invoke_hook
        except Exception:
            invoke_hook = None

        children_cost_total = 0.0
        for entry in results:
            child_role = entry.pop("_child_role", None)
            child_cost = entry.pop("_child_cost_usd", 0.0)
            try:
                if child_cost:
                    children_cost_total += float(child_cost)
            except (TypeError, ValueError):
                pass
            if invoke_hook is None:
                continue
            try:
                child_index = entry.get("task_index", -1)
                child = child_by_index.get(child_index)
                invoke_hook(
                    "subagent_stop",
                    parent_session_id=parent_session_id,
                    parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "",
                    child_session_id=getattr(child, "session_id", None),
                    child_role=child_role,
                    child_summary=entry.get("summary"),
                    child_status=entry.get("status"),
                    tool_call_history=_subagent_stop_tool_call_history(
                        entry.get("tool_trace")
                    ),
                    duration_ms=int((entry.get("duration_seconds") or 0) * 1000),
                )
            except Exception:
                logger.debug("subagent_stop hook invocation failed", exc_info=True)

        if children_cost_total > 0.0:
            try:
                current = float(
                    getattr(parent_agent, "session_estimated_cost_usd", 0.0) or 0.0
                )
                parent_agent.session_estimated_cost_usd = current + children_cost_total
                if getattr(parent_agent, "session_cost_source", "none") in {
                    None,
                    "",
                    "none",
                }:
                    parent_agent.session_cost_source = "subagent"
                if getattr(parent_agent, "session_cost_status", "unknown") in {
                    None,
                    "",
                    "unknown",
                }:
                    parent_agent.session_cost_status = "estimated"
            except Exception:
                logger.debug("Subagent cost rollup failed", exc_info=True)

        return completion_metadata




def _recover_tasks_from_json_string(
    tasks: Any,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(tasks, str):
        return None, None
    raw = tasks.strip()
    if not raw:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "tasks must be a JSON array of task objects; received a string "
            f"that could not be parsed as JSON ({exc.msg})."
        )
    if not isinstance(parsed, list):
        return None, (
            f"tasks must be a JSON array of task objects; parsed "
            f"{type(parsed).__name__} instead."
        )
    return parsed, None


# Placeholder shapes for batch goal validation: bare 'TODO', bare 'task N'
# labels, or goals still carrying unexpanded template markers.
#
# The marker regex is deliberately NARROW: it only fires on snake_case /
# space-separated placeholder identifiers (`<feature_name>`, `{file path}`,
# `<FEATURE-NAME>`) — the shape LLM templates actually leave behind. Bare
# single-word brackets are left alone because legitimate coding goals are
# full of them: generics (`Vec<T>`, `Result<String>`), HTML tags (`<div>`),
# JSON/dict snippets (`{"key": 1}`), glob braces (`{a,b}`), and f-string
# style (`{i}`) must never be rejected (post-merge audit of #81141).
_PLACEHOLDER_GOAL_RE = re.compile(r"^(todo|task\s*\d+)$", re.IGNORECASE)
_TEMPLATE_MARKER_RE = re.compile(
    r"<[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+)+>"
    r"|\{[A-Za-z][A-Za-z0-9]*(?:[ _-][A-Za-z0-9]+)+\}"
)
_MIN_BATCH_GOAL_LEN = 10


def _validate_batch_tasks(task_list: List[Dict[str, Any]]) -> Optional[str]:
    """Validate a tasks=[...] batch beyond per-task goal presence.

    Returns an actionable error string, or None when the batch is valid.
    Batch-only by design: the single-`goal` form legitimately uses short
    goals, so these checks must never run on it.

    Duplicate goals are deliberately NOT rejected: identical-goal fan-outs
    are a legitimate pattern (best-of-N / ensemble sampling), and blocking
    them broke real workflows (post-merge audit of #81141).
    """
    if len(task_list) < 2:
        return (
            "Batch mode requires at least 2 tasks. For a single task, use "
            "the `goal` parameter instead of `tasks`: "
            'delegate_task(goal="...", context="...").'
        )

    for i, task in enumerate(task_list):
        goal = str(task.get("goal", "")).strip()
        normalized = " ".join(goal.lower().split())

        if _PLACEHOLDER_GOAL_RE.match(normalized):
            return (
                f"Task {i} has a placeholder goal ({goal!r}). Replace it "
                "with a specific, self-contained description of what the "
                "subagent should accomplish."
            )
        marker = _TEMPLATE_MARKER_RE.search(goal)
        if marker:
            return (
                f"Task {i} goal contains an unexpanded template marker "
                f"({marker.group(0)!r}). Substitute the real value before "
                "calling delegate_task — subagents cannot resolve "
                "placeholders."
            )
        if len(goal) < _MIN_BATCH_GOAL_LEN:
            return (
                f"Task {i} goal is too short ({goal!r}). Write a specific, "
                "self-contained goal of at least "
                f"{_MIN_BATCH_GOAL_LEN} characters so the subagent knows "
                "exactly what to do."
            )
    return None



def _build_children(
    task_list: List[Dict[str, Any]], task_schemas: List[Optional[Dict[str, Any]]], creds: Dict[str, Any], *,
    top_role: str, max_iterations: int, parent_agent, routing_cfg: Dict[str, Any],
    live_deleg_id: Optional[str], live_writers: list, profile_toolsets: Optional[List[str]] = None,
) -> tuple[List[tuple], Optional[str]]:
    """Build every child on the main thread (construction is not thread-safe);
    ``(children, None)`` or ``([], error)`` on an explicit-pin preflight failure."""
    from tools.delegation_live_log import wrap_progress_callback
    from tools.delegation_output_schema import append_output_contract
    overrides = {
        "override_provider": creds["provider"], "override_base_url": creds["base_url"],
        "override_api_key": creds["api_key"], "override_api_mode": creds["api_mode"],
        "override_request_overrides": creds.get("request_overrides"),
        "override_acp_command": creds.get("command"),
        "override_acp_args": creds.get("args"),
        "routing_cfg": routing_cfg,
    }
    children = []
    for i, t in enumerate(task_list):
        _task_schema = task_schemas[i] if i < len(task_schemas) else None
        _child_context = t.get("context")
        if _task_schema is not None:
            _child_context = append_output_contract(_child_context, _task_schema)
        try:
            child = _build_child_preserving_parent_tools(
                task_index=i, goal=t["goal"], context=_child_context,
                toolsets=profile_toolsets,
                model=creds["model"], max_iterations=max_iterations, task_count=len(task_list),
                parent_agent=parent_agent, role=_normalize_role(t.get("role") or top_role), **overrides,
            )
        except ValueError as exc:
            return [], str(exc)
        if _task_schema is not None:
            with _quiet("Could not attach output schema to child %d", i):
                child._delegate_output_schema = _task_schema
        # Tee progress events into the live transcript (wrapper keeps the
        # _flush contract and swallows writer failures).
        _writer = live_writers[i] if i < len(live_writers) else None
        if _writer is not None:
            child.tool_progress_callback = wrap_progress_callback(getattr(child, "tool_progress_callback", None), _writer)
            child._live_transcript_path = str(_writer.path)
        if live_deleg_id:
            setattr(child, "_delegation_id", live_deleg_id)
            _ident_ref = getattr(child, "_progress_identity_ref", None)
            if isinstance(_ident_ref, dict):
                _ident_ref["delegation_id"] = live_deleg_id
        children.append((i, t, child))
    return children, None


def _run_single_child(
    task_index: int, goal: str, child=None, parent_agent=None, *, owner_session_id: Optional[str] = None,
    owner_transport: Any = None, owner_session_record: Any = None, **_kwargs,
) -> Dict[str, Any]:
    """Run a pre-built child agent through the decomposed lifecycle owner."""
    child_progress_cb = getattr(child, "tool_progress_callback", None)
    child_pool, leased_cred_id = _lease_child_credential(child)
    heartbeat = _start_heartbeat(child, parent_agent, task_index)
    subagent_id = _register_child(
        child, parent_agent, goal, owner_session_id=owner_session_id,
        owner_transport=owner_transport, owner_session_record=owner_session_record,
    )
    run = _ChildRun(child, parent_agent, task_index, goal, subagent_id, child_progress_cb)
    close_deferred = False
    try:
        heartbeat.start()
        _safe_progress(child_progress_cb, "subagent.start", preview=goal)
        run.seed_workspace()
        result, failure, close_deferred = run.await_child()
        if failure is not None:
            if failure.get("status") == "error" and failure.get("failure_reason"):
                failure["status"] = "failed"
            return failure
        schema = _validate_child_output_schema(child, result or {}, task_index, run.child_task_id, run.relay_text)
        _merge_late_steer(result, subagent_id, child)
        if child_progress_cb and hasattr(child_progress_cb, "_flush"):
            with _quiet("Progress callback flush failed: %s"):
                child_progress_cb._flush()
        duration = run.elapsed()
        entry = _build_result_entry(child, result or {}, task_index, run.elapsed(), schema)
        if entry.get("status") == "failed":
            classified_reason = (result or {}).get("failure_reason")
            if classified_reason and schema.valid is not None:
                entry["exit_reason"] = str(classified_reason)
        if schema.valid is not None:
            metadata = _validated_completion_metadata(
                (result or {}).get("final_response", ""),
                schema_valid=schema.valid,
                exit_reason=(result or {}).get("failure_reason") or entry["exit_reason"],
            )
            if metadata:
                entry["_completion_metadata"] = metadata
                entry["exit_reason"] = metadata.get("exit_reason", entry["exit_reason"])
                entry["truncated"] = entry["exit_reason"] == "max_iterations"
        run.append_sibling_write_reminder(entry)
        run.account_background_processes(entry)
        run.emit_complete(result or {}, entry, duration)
        return run.attach_worktree(entry)
    except Exception as exc:
        late_pending_steer = run.close_steering()
        logging.exception("[subagent-%s] failed", task_index)
        return run.finish_failed(
            _fabricated_entry(task_index, "error", str(exc), child, run.elapsed()), late_pending_steer,
            preview=str(exc), summary=str(exc), status="failed",
        )
    finally:
        run.cleanup(
            heartbeat=heartbeat,
            child_pool=child_pool,
            leased_cred_id=leased_cred_id,
            close_deferred=close_deferred,
        )


def delegate_task(
    goal: Optional[str] = None, context: Optional[str] = None, tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None, role: Optional[str] = None, background: Optional[bool] = None,
    output_schema: Optional[Dict[str, Any]] = None, tool_profile: Optional[str] = None,
    _auto_continue: Optional[bool] = False, action: Optional[str] = None, subagent_id: Optional[str] = None,
    message: Optional[str] = None, parent_agent=None, credentials_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Spawn child agents (single ``goal`` or ``tasks=[...]`` batch) or control running ones. ``action``
    list/steer/stop run synchronously and bypass the pause gate, depth limit and async dispatch. ``role`` is legacy
    (per-task beats top-level; capability is depth-derived). Returns JSON with one results entry per task, or a
    dispatch handle when running in the background."""
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    normalized_action = (action or "").strip().lower()
    if normalized_action in _CONTROL_ACTIONS:
        return _handle_control_action(normalized_action, subagent_id, message, parent_agent)
    if normalized_action and normalized_action != "spawn":
        return tool_error(f"Unknown action '{action}'. Use spawn (default), list, steer, or stop.")

    # Operator kill switch (TUI / delegation.pause RPC): blocks NEW spawns only.
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    top_role = _normalize_role(role)
    # background applies to single tasks AND batches: a batch is ONE async unit
    # that joins on every child and re-enters as a single consolidated message.
    background = is_truthy_value(background, default=False) if background is not None else False

    depth = getattr(parent_agent, "_delegate_depth", 0)
    max_spawn = _get_max_spawn_depth()
    if depth >= max_spawn:
        return tool_error(
            f"Delegation depth limit reached (depth={depth}, max_spawn_depth={max_spawn}). Raise "
            f"delegation.max_spawn_depth in config.yaml if deeper nesting is required (no hard ceiling, but each level "
            f"multiplies API cost)."
        )

    cfg = _load_config()
    try:
        profile_toolsets = _resolve_tool_profile(tool_profile, cfg)
    except ValueError as exc:
        return tool_error(str(exc))
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    effective_max_iter = default_max_iter
    # Caller-supplied max_iterations is ignored: the config value is authoritative
    # so budgets stay predictable (kwarg kept for internal callers/tests).
    if max_iterations is not None and max_iterations != default_max_iter:
        logger.debug(
            "delegate_task: ignoring caller-supplied max_iterations=%s; using delegation.max_iterations=%s from config",
            max_iterations, default_max_iter,
        )
    # credentials_cfg (internal callers only, e.g. /review → auxiliary.review) is
    # a per-call routing owner shaped like the delegation config section. Keep
    # the route and its fallback policy together through child construction.
    routing_cfg = credentials_cfg if credentials_cfg is not None else cfg
    try:
        creds = _resolve_delegation_credentials(routing_cfg, parent_agent)
    except ValueError as exc:
        # Explicit-pin preflight failures (e.g. pinned delegation.command missing from PATH) refuse the
        # spawn loudly (#80450).
        return tool_error(str(exc))
    max_children = _get_max_concurrent_children()
    task_list, err = _normalize_task_list(goal, context, tasks, output_schema, top_role, max_children)
    if not err:
        task_schemas, err = _coerce_task_schemas(task_list, output_schema)
    if err:
        return tool_error(err)
    n_tasks = len(task_list)

    overall_start = time.monotonic()
    # Live transcripts: cache/delegation/live/<id>/task-<n>.log per task, a side channel with zero effect on message
    # content or prompt caching. Best-effort: on failure live_paths is empty and delegation proceeds.
    from tools.delegation_live_log import create_live_transcripts
    live_deleg_id, live_writers, live_paths = create_live_transcripts(
        task_list, context, model=creds.get("model"), provider=creds.get("provider")
    )
    _announce_batch(parent_agent, len(task_list), live_deleg_id)
    origin = _capture_origin()

    # Only the BacklinkHub single-site worker gets host-owned continuation.
    # Keeping this decision here (after schema coercion, before construction)
    # means a stale model argument can never enable the loop accidentally.
    _auto_continue_enabled = bool(
        _auto_continue
        and background
        and tool_profile == "backlinkhub"
        and n_tasks == 1
        and task_schemas
        and task_schemas[0] is not None
    )

    logger.info(
        "delegate_task continuation config: tool_profile=%s "
        "_auto_continue=%s _auto_continue_enabled=%s background=%s "
        "task_count=%d schema_attached=%s",
        tool_profile or "",
        bool(_auto_continue),
        _auto_continue_enabled,
        background,
        n_tasks,
        bool(task_schemas and task_schemas[0] is not None),
    )

    # Capture the ORIGINATING session's wake target BEFORE any child agent is
    # constructed: _build_child_agent() -> AIAgent() -> agent_init calls
    # set_current_session_id(child.session_id), which clobbers the
    # HERMES_SESSION_ID ContextVar and os.environ with the subagent's internal
    # id before the background-dispatch code below would read it. The
    # request-scoped chat_id binding (the raw X-Hermes-Session-Id on
    # api_server) is untouched by child construction, so read it here and
    # thread it through the dispatch.
    from tools.async_delegation import _current_origin_session_id

    _origin_wake_sid = _current_origin_session_id()
    try:
        from gateway.session_context import get_session_env

        _origin_ui_session_id = get_session_env("HERMES_UI_SESSION_ID", "")
    except Exception:
        _origin_ui_session_id = ""
    _origin_owner_transport, _origin_owner_session_record = (
        _capture_gateway_steer_authority(_origin_ui_session_id)
    )
    _continuation_requested = bool(_auto_continue and background and tool_profile == "backlinkhub" and len(task_list) == 1 and task_schemas and task_schemas[0] is not None)
    from tools.delegation_live_log import update_manifest_statuses
    children, err = ([], None) if _continuation_requested else _build_children(
        task_list, task_schemas, creds, top_role=top_role, max_iterations=default_max_iter, parent_agent=parent_agent,
        routing_cfg=routing_cfg, live_deleg_id=live_deleg_id, live_writers=live_writers,
        profile_toolsets=profile_toolsets,
    )
    if err:
        return tool_error(err)
    batch = _Batch(
        task_list, children, parent_agent, creds, context, top_role, max_children,
        live_deleg_id, live_writers, live_paths, *origin, overall_start,
    )
    # The legacy continuation loop below is intentionally reached only for the
    # BacklinkHub schema path; ordinary delegation stays on the vendor batch
    # dispatcher above.
    if not _auto_continue_enabled:
        return _run_batch(batch, background)

    def _prepare_child_for_task(
        task_index: int,
        task: Dict[str, Any],
        *,
        goal_override: Optional[str] = None,
    ):
        """Construct one child with the same contract as the first segment.

        Continuation segments are built lazily after the previous child has
        been fully finalized.  Keeping construction in one helper prevents a
        resumed Luna from losing the tool profile, output schema, live log, or
        delegation identity.
        """
        effective_role = _normalize_role(task.get("role") or top_role)
        child_goal = goal_override or task["goal"]
        task_schema = task_schemas[task_index] if task_index < len(task_schemas) else None
        child_context = task.get("context")
        if task_schema is not None:
            from tools.delegation_output_schema import append_output_contract

            child_context = append_output_contract(child_context, task_schema)
        from tools.delegation_live_log import wrap_progress_callback

        child = _build_child_preserving_parent_tools(
            task_index=task_index,
            goal=child_goal,
            context=child_context,
            # Raw toolsets are never model-facing. A named profile is
            # operator-defined in config and is still intersected with the
            # parent's effective toolsets inside _build_child_agent.
            toolsets=profile_toolsets,
            model=creds["model"],
            max_iterations=effective_max_iter,
            task_count=n_tasks,
            parent_agent=parent_agent,
            override_provider=creds["provider"],
            override_base_url=creds["base_url"],
            override_api_key=creds["api_key"],
            override_api_mode=creds["api_mode"],
            override_request_overrides=creds.get("request_overrides"),
            override_acp_command=creds.get("command"),
            override_acp_args=creds.get("args"),
            routing_cfg=routing_cfg,
            role=effective_role,
        )
        if task_schema is not None:
            try:
                child._delegate_output_schema = task_schema
            except Exception:
                logger.debug("Could not attach output schema to child %d", task_index)
        # Tee progress into the one task log.  A continuation appends to the
        # same log, so the operator gets one complete audit trail per task.
        writer = live_writers[task_index] if task_index < len(live_writers) else None
        if writer is not None:
            child.tool_progress_callback = wrap_progress_callback(
                getattr(child, "tool_progress_callback", None), writer
            )
            child._live_transcript_path = str(writer.path)
        if live_deleg_id:
            setattr(child, "_delegation_id", live_deleg_id)
        return child

    # Build all initial child agents on the calling thread.  The preserving
    # wrapper serializes global tool-name resolution while keeping the parent
    # tool list intact.
    children = []
    for i, t in enumerate(task_list):
        try:
            child = _prepare_child_for_task(i, t)
        except ValueError as exc:
            # Explicit-pin preflight failures (e.g. pinned delegation.command
            # missing from PATH) refuse the spawn loudly (#80450).
            return tool_error(str(exc))
        children.append((i, t, child))
    batch.children = children

    # The async unit may replace its single child after a native iteration
    # boundary.  Keep the interrupt/progress closures pointed at this mutable
    # reference instead of the child that happened to exist at dispatch time.
    _active_child_refs = [c for _, _, c in children]
    _active_child_refs_lock = threading.RLock()
    _continuation_abort = threading.Event()

    def _execute_and_aggregate(*, honor_parent_interrupt: bool = True) -> dict:
        """Run all built children (1 or N), join on them, aggregate results,
        fire subagent_stop hooks + cost rollup, and return the combined result
        dict. Used by BOTH the synchronous path and the background runner. In
        the background case this whole function runs on the daemon executor, so
        the parent turn isn't blocked — but the batch still JOINS on itself
        here (all children must finish) before producing ONE consolidated
        results block. That is the contract: fan-out runs in the background,
        waits on each other, and returns together.
        """
        results = []
        if n_tasks == 1:
            # Single task -- run directly (no thread pool overhead)
            _i, _t, child = children[0]
            result = _run_single_child(
                _i,
                _t["goal"],
                child,
                parent_agent,
                owner_session_id=_origin_ui_session_id or None,
                owner_transport=_origin_owner_transport,
                owner_session_record=_origin_owner_session_record,
            )
            results.append(result)
        else:
            # Batch -- run in parallel with per-task progress lines
            completed_count = 0
            spinner_ref = getattr(parent_agent, "_delegate_spinner", None)
            task_labels = [str(task.get("goal") or "") for task in task_list]

            # Daemon workers (tools.daemon_pool): the `with` block still joins
            # normally, but if the parent is interrupted while a child is
            # wedged, the abandoned worker must not block interpreter exit.
            from tools.daemon_pool import DaemonThreadPoolExecutor
            with DaemonThreadPoolExecutor(max_workers=max_children) as executor:
                futures = {}
                for i, t, child in children:
                    child_context = contextvars.copy_context()
                    future = executor.submit(
                        child_context.run,
                        _run_single_child,
                        task_index=i,
                        goal=t["goal"],
                        child=child,
                        parent_agent=parent_agent,
                        owner_session_id=_origin_ui_session_id or None,
                        owner_transport=_origin_owner_transport,
                        owner_session_record=_origin_owner_session_record,
                    )
                    futures[future] = i

                # Poll futures with interrupt checking.  as_completed() blocks
                # until ALL futures finish — if a child agent gets stuck,
                # the parent blocks forever even after interrupt propagation.
                # Instead, use wait() with a short timeout so we can bail
                # when the parent is interrupted.
                # Map task_index -> child agent, so fabricated entries for
                # still-pending futures can carry the correct _delegate_role.
                _child_by_index = {i: child for (i, _, child) in children}

                pending = set(futures.keys())
                while pending:
                    if (
                        honor_parent_interrupt
                        and getattr(parent_agent, "_interrupt_requested", False) is True
                    ):
                        # Parent interrupted — collect whatever finished and
                        # abandon the rest.  Children already received the
                        # interrupt signal; we just can't wait forever.
                        for f in pending:
                            idx = futures[f]
                            if f.done():
                                try:
                                    entry = f.result()
                                except Exception as exc:
                                    entry = {
                                        "task_index": idx,
                                        "status": "error",
                                        "summary": None,
                                        "error": str(exc),
                                        "api_calls": 0,
                                        "duration_seconds": 0,
                                        "_child_role": getattr(
                                            _child_by_index.get(idx), "_delegate_role", None
                                        ),
                                    }
                            else:
                                entry = {
                                    "task_index": idx,
                                    "status": "interrupted",
                                    "summary": None,
                                    "error": "Parent agent interrupted — child did not finish in time",
                                    "api_calls": 0,
                                    "duration_seconds": 0,
                                    "_child_role": getattr(
                                        _child_by_index.get(idx), "_delegate_role", None
                                    ),
                                }
                            results.append(entry)
                            completed_count += 1
                        break

                    from concurrent.futures import wait as _cf_wait, FIRST_COMPLETED

                    done, pending = _cf_wait(
                        pending, timeout=0.5, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        try:
                            entry = future.result()
                        except Exception as exc:
                            idx = futures[future]
                            entry = {
                                "task_index": idx,
                                "status": "error",
                                "summary": None,
                                "error": str(exc),
                                "api_calls": 0,
                                "duration_seconds": 0,
                                "_child_role": getattr(
                                    _child_by_index.get(idx), "_delegate_role", None
                                ),
                            }
                        results.append(entry)
                        completed_count += 1

                        # Print per-task completion line above the spinner
                        idx = entry["task_index"]
                        label = (
                            task_labels[idx] if idx < len(task_labels) else f"Task {idx}"
                        )
                        dur = entry.get("duration_seconds", 0)
                        status = entry.get("status", "?")
                        icon = "✓" if status == "completed" else "✗"
                        remaining = n_tasks - completed_count
                        completion_line = f"{icon} [{idx+1}/{n_tasks}] {label}  ({dur}s)"
                        if spinner_ref:
                            try:
                                spinner_ref.print_above(completion_line)
                            except Exception:
                                _emit_parent_console(parent_agent, f"  {completion_line}")
                        else:
                            _emit_parent_console(parent_agent, f"  {completion_line}")

                        # Update spinner text to show remaining count
                        if spinner_ref and remaining > 0:
                            try:
                                spinner_ref.update_text(
                                    f"🔀 {remaining} task{'s' if remaining != 1 else ''} remaining"
                                )
                            except Exception as e:
                                logger.debug("Spinner update_text failed: %s", e)

            # Sort by task_index so results match input order
            results.sort(key=lambda r: r["task_index"])

        # Cap subagent summaries against the parent's remaining context
        # headroom (split across the batch) before they enter the parent's
        # conversation. Full text is spilled to disk so nothing is lost.
        # Covers both the single-task and batch paths. See PR #9126.
        completion_metadata = _finalize_child_results(
            results, task_list, children, parent_agent
        )

        total_duration = round(time.monotonic() - overall_start, 2)

        # Close out the live transcripts: terminal marker per task + manifest
        # status update. The files are retained (retention pruning happens on
        # future dispatches) — they double as the full-fidelity operational
        # record alongside the summary spill files.
        for entry in results:
            _idx = entry.get("task_index", -1)
            _w = (
                live_writers[_idx]
                if isinstance(_idx, int) and 0 <= _idx < len(live_writers)
                else None
            )
            if _w is not None:
                try:
                    _w.finalize(entry)
                except Exception:
                    logger.debug("Live transcript finalize failed", exc_info=True)
                if _idx < len(live_paths):
                    entry["live_transcript"] = live_paths[_idx]
        # A BacklinkHub single-task run may continue with another child inside
        # this same async delegation.  Do not mark its live manifest terminal
        # until the continuation loop has finished.
        if not _auto_continue_enabled:
            update_manifest_statuses(live_deleg_id, results)

        combined: Dict[str, Any] = {
            "results": results,
            "total_duration_seconds": total_duration,
        }
        if any(completion_metadata):
            # Keep this only inside the host-side handoff.  The async transport
            # consumes it to build a continuation signal even when a summary
            # was shortened; synchronous callers strip it before serialization.
            combined["_completion_metadata"] = completion_metadata
        if live_paths:
            combined["live_transcripts"] = list(live_paths)
        return combined

    def _run_single_continuation_segment(
        child: Any,
        task: Dict[str, Any],
        goal: str,
    ) -> Dict[str, Any]:
        """Run and host-finalize one lazily-created single-task segment.

        This intentionally mirrors the single-task branch above instead of
        re-entering ``delegate_task``.  The latter would create a second async
        record and hand the decision back to the parent model, which is the
        failure mode this continuation path removes.
        """
        segment_start = time.monotonic()
        segment_results: List[Dict[str, Any]] = []
        result = _run_single_child(
            0,
            goal,
            child,
            parent_agent,
            owner_session_id=_origin_ui_session_id or None,
            owner_transport=_origin_owner_transport,
            owner_session_record=_origin_owner_session_record,
            _continuation_segment=True,
        )
        segment_results.append(result)
        segment_metadata = _finalize_child_results(
            segment_results,
            [task],
            [(0, task, child)],
            parent_agent,
        )
        if isinstance(segment_metadata, dict):
            for item in segment_metadata.get("results", []):
                if item.get("status") == "error" and item.get("failure_reason"):
                    item["status"] = "failed"

        writer = live_writers[0] if live_writers else None
        if writer is not None:
            try:
                writer.finalize(result)
            except Exception:
                logger.debug("Continuation live transcript finalize failed", exc_info=True)
            if live_paths:
                result["live_transcript"] = live_paths[0]

        combined: Dict[str, Any] = {
            "results": segment_results,
            "total_duration_seconds": round(time.monotonic() - segment_start, 2),
        }
        if any(segment_metadata):
            combined["_completion_metadata"] = segment_metadata
        if live_paths:
            combined["live_transcripts"] = list(live_paths)
        return combined

    def _single_completion_metadata(combined: Any) -> Dict[str, Any]:
        """Extract the private, schema-validated metadata for one segment."""
        if not isinstance(combined, dict):
            return {}
        metadata = combined.get("_completion_metadata")
        if isinstance(metadata, list) and metadata and isinstance(metadata[0], dict):
            return dict(metadata[0])
        return {}

    def _continuation_goal(metadata: Dict[str, Any]) -> str:
        """Build a compact next-segment prompt without replaying history."""
        site_id = str(metadata.get("site_id") or "")
        run_id = str(metadata.get("run_id") or "")
        target = metadata.get("target")
        space_id = metadata.get("ego_task_space_id")
        cleanup = str(metadata.get("ego_cleanup") or "").strip().lower()
        if cleanup == "closed":
            space_note = (
                "旧空间已关闭；浏览当前候选时按参考核验并创建同一轮次的替代空间。"
            )
        elif cleanup == "not_created" or not isinstance(space_id, int):
            space_note = (
                "本轮尚无 Ego 空间；浏览当前候选时创建一个并保存数字 ID。"
            )
        else:
            space_note = (
                f"首个浏览器事务复用数字 Ego 空间 {space_id}，本段继续时不要提前关闭。"
            )
        target_note = (
            f"本轮显式 target_count={target}；首次 advance 必须传 target_count={target}，"
            "不得回退到站点 daily_quota。"
            if isinstance(target, int) and not isinstance(target, bool) and target > 0
            else "本轮没有可验证的显式 target_count；仅在 BacklinkHub 已持久化目标时省略该参数。"
        )
        return (
            "继续：复用 BacklinkHub 同一外链轮次，不创建新轮次。"
            f"site_id={site_id}；run_id={run_id}；target={target}；"
            f"ego_task_space_id={space_id}；ego_cleanup={cleanup}。{target_note}"
            "先用 skill_view(name=\"backlink-round-execution\", "
            "file_path=\"references/luna-worker.md\") 加载叶子参考一次，再用 "
            "skill_view(name=\"ego-browser\") 加载官方技能一次，不得重复加载。"
            "随后第一项业务调用必须是同一 run_id、site_id 的 "
            "backlinkhub_advance_submission_round；若上面给出显式 target_count，必须原样传入，"
            "否则由 BacklinkHub 恢复已持久化目标和未回写候选。"
            f"{space_note}之后严格执行参考中的 "
            "ADVANCE -> BROWSE -> RECORD -> ADVANCE；不重复最终提交。"
        )

    def _detach_child_from_parent(child: Any) -> None:
        """Remove a detached async child from foreground interrupt ownership."""
        if not hasattr(parent_agent, "_active_children"):
            return
        try:
            lock = getattr(parent_agent, "_active_children_lock", None)
            if lock:
                with lock:
                    parent_agent._active_children.remove(child)
            else:
                parent_agent._active_children.remove(child)
        except (ValueError, AttributeError):
            pass

    def _run_auto_continuation(
        *, honor_parent_interrupt: bool = False,
    ) -> Dict[str, Any]:
        """Keep one BacklinkHub site task alive across native child boundaries."""
        from tools.delegation_output_schema import (
            completion_can_continue,
            continuation_progress_fingerprint,
            failed_segment_can_continue,
        )

        segment_history: List[Dict[str, Any]] = []
        cumulative_api_calls = 0
        cumulative_input = 0
        cumulative_output = 0
        cumulative_cost = 0.0
        cumulative_duration = 0.0
        cumulative_reasoning = 0
        last_safe_metadata: Dict[str, Any] = {}
        transport_recovery_fingerprints: set = set()
        transport_recovery_exhausted = False
        combined: Dict[str, Any] = {}

        def _continuation_decision(metadata: Dict[str, Any]) -> bool:
            can_continue = completion_can_continue(
                metadata,
                exit_reason=metadata.get("exit_reason"),
                schema_valid=metadata.get("schema_valid"),
            )
            logger.info(
                "delegate_task continuation decision: tool_profile=%s "
                "_auto_continue=%s _auto_continue_enabled=%s segment=%d "
                "schema_valid=%s exit_reason=%s stop_reason=%s "
                "segment_iteration_boundary=%s ego_cleanup=%s "
                "candidate_external_side_effect=%s remaining=%s "
                "can_continue=%s",
                tool_profile or "",
                bool(_auto_continue),
                _auto_continue_enabled,
                len(segment_history),
                metadata.get("schema_valid"),
                metadata.get("exit_reason"),
                metadata.get("stop_reason"),
                metadata.get("segment_iteration_boundary"),
                metadata.get("ego_cleanup"),
                metadata.get("candidate_external_side_effect"),
                metadata.get("remaining"),
                can_continue,
            )
            return can_continue

        def _clear_active_child_refs() -> None:
            with _active_child_refs_lock:
                _active_child_refs.clear()

        def _record_segment(segment: Dict[str, Any]) -> None:
            nonlocal cumulative_api_calls, cumulative_input
            nonlocal cumulative_output, cumulative_cost
            nonlocal cumulative_duration, cumulative_reasoning
            entries = segment.get("results") if isinstance(segment, dict) else None
            entry = entries[0] if isinstance(entries, list) and entries else {}
            if not isinstance(entry, dict):
                entry = {}
            try:
                cumulative_api_calls += int(entry.get("api_calls", 0) or 0)
            except (TypeError, ValueError):
                pass
            tokens = entry.get("tokens") if isinstance(entry.get("tokens"), dict) else {}
            try:
                cumulative_input += int(tokens.get("input", 0) or 0)
            except (TypeError, ValueError):
                pass
            try:
                cumulative_output += int(tokens.get("output", 0) or 0)
            except (TypeError, ValueError):
                pass
            try:
                cumulative_cost += float(entry.get("cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                cumulative_duration += float(entry.get("duration_seconds", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                cumulative_reasoning += int(
                    entry.get("reasoning_tokens", 0)
                    or (tokens.get("reasoning", 0) if isinstance(tokens, dict) else 0)
                    or 0
                )
            except (TypeError, ValueError):
                pass
            metadata = _single_completion_metadata(segment)
            history_entry: Dict[str, Any] = {
                "segment": len(segment_history) + 1,
                "status": entry.get("status"),
                "exit_reason": entry.get("exit_reason"),
                "api_calls": entry.get("api_calls", 0),
                "duration_seconds": entry.get("duration_seconds", 0),
                "reasoning_tokens": entry.get("reasoning_tokens", 0),
            }
            for key in (
                "published",
                "pending",
                "attempted_unconfirmed",
                "failed_retryable",
                "failed_final",
                "remaining",
                "queue_exhausted",
                "target_reached",
                "candidate_external_side_effect",
            ):
                if key in metadata:
                    history_entry[key] = metadata[key]
            segment_history.append(history_entry)

        # Keep the first segment and every continuation in one guarded block.
        # If construction or execution raises, the finally block below still
        # drops the mutable interrupt reference and closes a child that was
        # constructed but never started.
        _unstarted_child = None
        try:
            # The first segment uses the normal aggregator, preserving all
            # existing lifecycle hooks and result formatting.
            combined = _execute_and_aggregate(
                honor_parent_interrupt=honor_parent_interrupt,
            )
            _record_segment(combined)
            metadata = _single_completion_metadata(combined)
            if metadata:
                last_safe_metadata = dict(metadata)

            expected_site_id = str(metadata.get("site_id") or "")
            expected_run_id = str(metadata.get("run_id") or "")
            expected_target = metadata.get("target")
            previous_fingerprint = continuation_progress_fingerprint(metadata)

            while _continuation_decision(metadata):
                if _continuation_abort.is_set() or (
                    honor_parent_interrupt
                    and getattr(parent_agent, "_interrupt_requested", False) is True
                ):
                    break
                # Never let a malformed worker switch the task to another site,
                # round, or target while carrying an existing async record. A
                # closed/missing Ego space may legitimately be replaced; space
                # continuity is checked after the next segment returns.
                if (
                    not expected_site_id
                    or not expected_run_id
                    or str(metadata.get("site_id") or "") != expected_site_id
                    or str(metadata.get("run_id") or "") != expected_run_id
                    or str(metadata.get("target")) != str(expected_target)
                ):
                    combined["continuation_error"] = (
                        "worker returned a different BacklinkHub site, run_id, or target"
                    )
                    break

                previous_metadata = dict(metadata)
                continuation_task = dict(task_list[0])
                next_goal = _continuation_goal(metadata)
                continuation_task["goal"] = next_goal
                next_child = None
                segment_started = False
                try:
                    next_child = _prepare_child_for_task(
                        0,
                        continuation_task,
                        goal_override=next_goal,
                    )
                    _unstarted_child = next_child
                    if _continuation_abort.is_set() or (
                        honor_parent_interrupt
                        and getattr(parent_agent, "_interrupt_requested", False) is True
                    ):
                        # A stop can race with child construction. Do not start
                        # a freshly built segment after the owner cancelled it.
                        break
                    _detach_child_from_parent(next_child)
                    with _active_child_refs_lock:
                        _active_child_refs[:] = [next_child]
                    segment_started = True
                    _unstarted_child = None
                    next_combined = _run_single_continuation_segment(
                        next_child,
                        continuation_task,
                        next_goal,
                    )
                except Exception as exc:
                    logger.exception("BacklinkHub continuation segment failed")
                    combined["continuation_error"] = str(exc)
                    break
                finally:
                    # _run_single_child owns cleanup after a started segment;
                    # only close a child that never reached that boundary.
                    if (
                        next_child is not None
                        and not segment_started
                        and _unstarted_child is next_child
                    ):
                        try:
                            next_child.close()
                        except Exception:
                            logger.debug(
                                "Failed to close unstarted continuation child",
                                exc_info=True,
                            )
                        _unstarted_child = None

                combined = next_combined
                _record_segment(combined)
                metadata = _single_completion_metadata(combined)
                if metadata:
                    last_safe_metadata = dict(metadata)

                    if (
                        str(metadata.get("site_id") or "") != expected_site_id
                        or str(metadata.get("run_id") or "") != expected_run_id
                        or str(metadata.get("target")) != str(expected_target)
                    ):
                        combined["continuation_error"] = (
                            "worker returned a different BacklinkHub site, run_id, or target"
                        )
                        break

                    previous_cleanup = str(
                        previous_metadata.get("ego_cleanup") or ""
                    ).strip().lower()
                    if previous_cleanup in {
                        "preserved_for_continuation",
                        "preserved",
                        "open",
                        "active",
                        "kept",
                        "not_closed",
                        "reused",
                        "preserved_for_serial_continuation",
                    } and metadata.get("ego_task_space_id") != previous_metadata.get(
                        "ego_task_space_id"
                    ):
                        combined["continuation_error"] = (
                            "worker changed a preserved Ego task space between segments"
                        )
                        break

                if not metadata:
                    entries = (
                        combined.get("results") if isinstance(combined, dict) else None
                    )
                    failed_entry = (
                        entries[0]
                        if isinstance(entries, list)
                        and entries
                        and isinstance(entries[0], dict)
                        else {}
                    )
                    if failed_segment_can_continue(failed_entry, last_safe_metadata):
                        recovery_fingerprint = continuation_progress_fingerprint(
                            last_safe_metadata
                        )
                        if (
                            recovery_fingerprint is not None
                            and recovery_fingerprint
                            not in transport_recovery_fingerprints
                        ):
                            transport_recovery_fingerprints.add(recovery_fingerprint)
                            if segment_history:
                                segment_history[-1]["transport_recovery"] = "scheduled"
                            metadata = dict(last_safe_metadata)
                            logger.info(
                                "delegate_task scheduling one transport recovery: "
                                "segment=%d exit_reason=%s",
                                len(segment_history),
                                failed_entry.get("exit_reason"),
                            )
                            continue
                        transport_recovery_exhausted = True
                        combined["continuation_stop_reason"] = (
                            "transport_recovery_exhausted"
                        )
                        if segment_history:
                            segment_history[-1]["transport_recovery"] = "exhausted"
                        logger.info(
                            "delegate_task transport recovery exhausted at segment=%d",
                            len(segment_history),
                        )

                # A repeated account fingerprint means the child neither
                # recorded a candidate outcome nor advanced the queue. Stop
                # here instead of spinning up identical Luna/Ego segments.
                fingerprint = continuation_progress_fingerprint(metadata)
                if (
                    previous_fingerprint is not None
                    and fingerprint is not None
                    and fingerprint == previous_fingerprint
                ):
                    combined["continuation_stop_reason"] = "no_progress"
                    logger.info(
                        "delegate_task continuation stopped: no progress after "
                        "segment=%d",
                        len(segment_history),
                    )
                    break
                if fingerprint is not None:
                    previous_fingerprint = fingerprint
        finally:
            if _unstarted_child is not None:
                try:
                    _unstarted_child.close()
                except Exception:
                    logger.debug(
                        "Failed to close pending continuation child", exc_info=True
                    )
            _clear_active_child_refs()

        if (
            transport_recovery_exhausted
            and last_safe_metadata.get("candidate_external_side_effect") == "none"
        ):
            cleanup_result = _cleanup_empty_agent_ego_space(
                last_safe_metadata.get("ego_task_space_id")
            )
            combined["ego_recovery_cleanup"] = cleanup_result
            if cleanup_result.get("closed") is True:
                last_safe_metadata["ego_cleanup"] = "closed"

        entries = combined.get("results") if isinstance(combined, dict) else None
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            final_entry = entries[0]
            final_entry["continuation_segments"] = len(segment_history)
            final_entry["cumulative_api_calls"] = cumulative_api_calls
            final_entry["cumulative_tokens"] = {
                "input": cumulative_input,
                "output": cumulative_output,
            }
            final_entry["cumulative_cost_usd"] = round(cumulative_cost, 6)
            final_entry["cumulative_duration_seconds"] = round(
                cumulative_duration, 2
            )
            final_entry["cumulative_reasoning_tokens"] = cumulative_reasoning
            combined["continuation_history"] = segment_history
        if last_safe_metadata and not _single_completion_metadata(combined):
            combined["continuation_last_progress"] = last_safe_metadata
        if live_deleg_id:
            update_manifest_statuses(live_deleg_id, entries or [])
        return combined

    # ----- Background dispatch: run the WHOLE batch as one async unit -----
    # When background is true, the entire fan-out runs on the daemon executor
    # via a single async delegation. _execute_and_aggregate() joins on every
    # child and produces ONE consolidated results block, which re-enters the
    # conversation as a single message when ALL children finish. The chat is
    # not blocked in the meantime. This is the contract: dispatch N subagents,
    # keep chatting, get the combined summaries back together at the end.
    if background:
        from tools.async_delegation import dispatch_async_delegation_batch
        from tools.approval import get_current_session_key

        # Finite sessions cannot route a detached subagent result back to the
        # agent after their turn/process ends. This includes stateless HTTP
        # requests (#10760) and one-shot Kanban workers (#63169). Fall back to
        # SYNCHRONOUS execution so the result returns in this same turn instead
        # of handing out a handle with no durable consumer. Mirrors the
        # pool-at-capacity inline fallback below.
        try:
            from gateway.session_context import async_delivery_supported
            _async_ok = async_delivery_supported()
        except Exception:
            _async_ok = True

        _wake_sid = ""
        if not _async_ok:
            # The adapter itself cannot push, but if a raw session id is
            # bound (the API server always binds one — see
            # ApiServerAdapter._bind_api_server_session), gateway.wake can
            # still reach the session by self-POSTing /v1/chat/completions
            # with that id in X-Hermes-Session-Id once the batch completes.
            # Only fall back to forced-sync execution when there is truly no
            # session id to wake. Uses the origin captured before child
            # construction (see _origin_wake_sid above) — reading
            # HERMES_SESSION_ID here would return the subagent's internal id.
            _wake_sid = _origin_wake_sid
            if _wake_sid:
                logger.info(
                    "delegate_task: async delivery unsupported on this "
                    "session, but a session id is bound (%s) — dispatching "
                    "in the background and waking the session via self-post "
                    "when it completes instead of forcing synchronous "
                    "execution.",
                    _wake_sid,
                )
                _async_ok = True

        if not _async_ok:
            logger.info(
                "delegate_task: async delivery unsupported on this session "
                "runtime; running the batch synchronously instead."
            )
            _sync_runner = (
                _run_auto_continuation(honor_parent_interrupt=True)
                if _auto_continue_enabled
                else _execute_and_aggregate()
            )
            _sync_result = _strip_internal_completion_metadata(_sync_runner)
            if isinstance(_sync_result, dict):
                _sync_result["note"] = (
                    "background=true is not available in this session — it cannot "
                    "receive a detached subagent result after the turn ends (a "
                    "one-shot runner such as `hermes -z`, a cron job, a Kanban "
                    "worker, or a stateless HTTP endpoint). The subagent(s) ran "
                    "SYNCHRONOUSLY and the result is included above."
                )
            return json.dumps(_sync_result, ensure_ascii=False)

        _session_key = get_current_session_key(default="")
        try:
            from gateway.session_context import get_session_env

            _source = get_session_env("HERMES_SESSION_SOURCE", "")
            # Refresh from the same task-local source when available, but retain
            # the immutable value captured before child construction otherwise.
            _origin_ui_session_id = (
                get_session_env("HERMES_UI_SESSION_ID", "") or _origin_ui_session_id
            )
            # In desktop/TUI, the routable session key is the durable
            # AIAgent.session_id. Context compression can rotate that id during
            # the same turn before the TUI-side session dict is re-anchored;
            # if we capture the stale approval/session context key here, the
            # async completion becomes an orphan and any desktop poller may
            # consume it. Gateway chats are different: their session_key is the
            # platform conversation key (agent:main:...), so keep it there.
            if _source == "tui":
                _agent_session_id = str(getattr(parent_agent, "session_id", "") or "")
                if _agent_session_id:
                    _session_key = _agent_session_id
        except Exception:
            _source = ""
        if not _session_key:
            # CLI (single-process) path: the approval contextvar is only bound
            # during gateway/TUI turns and HERMES_SESSION_KEY is not in the CLI
            # environment, so the key resolves empty here. Since #64240 the CLI
            # drains completions through a positive-ownership filter keyed on
            # the durable AIAgent.session_id — an empty session_key would fail
            # closed and the CLI could never claim its own completions, while
            # a restored foreign event with an empty key could leak into any
            # unfiltered consumer (#64484). Stamp the parent's durable session
            # id instead; compression rotations are handled on the drain side
            # via resolve_resume_session_id lineage resolution.
            _agent_session_id = str(getattr(parent_agent, "session_id", "") or "")
            if _agent_session_id:
                _session_key = _agent_session_id
        _parent_session_id = getattr(parent_agent, "session_id", None)
        _child_agents = [c for (_, _, c) in children]

        # Detach every child from the parent's interrupt-propagation list — the
        # batch's lifecycle is owned by the async registry now, not the parent
        # turn. _build_child_agent attached them (correct for sync runs).
        if hasattr(parent_agent, "_active_children"):
            _ac_lock = getattr(parent_agent, "_active_children_lock", None)
            for _c in _child_agents:
                try:
                    if _ac_lock:
                        with _ac_lock:
                            parent_agent._active_children.remove(_c)
                    else:
                        parent_agent._active_children.remove(_c)
                except ValueError:
                    pass

        def _batch_runner():
            # This batch is detached from the foreground turn. Its lifecycle is
            # owned by the async registry and cancelled only via _batch_interrupt.
            if _auto_continue_enabled:
                return _run_auto_continuation(honor_parent_interrupt=False)
            return _execute_and_aggregate(honor_parent_interrupt=False)

        def _batch_interrupt():
            # Interrupt the segment that is actually running.  A continuation
            # can replace the child after the first segment has finalized.
            _continuation_abort.set()
            with _active_child_refs_lock:
                current_children = list(_active_child_refs)
            for _c in current_children:
                try:
                    interrupted = request_hard_interrupt(_c, "Async delegation cancelled")
                    if not interrupted and hasattr(_c, "_interrupt_requested"):
                        _c._interrupt_requested = True
                except Exception:
                    pass

        def _batch_progress():
            # Progress token for the async registry's stale monitor: the
            # combined (api_call_count, current_tool, last_activity_ts) of
            # every child. last_activity_ts is ticked by _touch_activity on
            # every streamed chunk ("receiving stream response"), every tool
            # transition, and every API-call start/completion — so a child
            # streaming a long response is alive even though api_call_count
            # only advances when the call completes (same liveness signal as
            # the compaction inactivity budget, PR #71508). A fully frozen
            # token past the stale threshold means the detached batch is
            # wedged (e.g. stuck inside the first model API call — #60203).
            # in_tool=True while ANY child is inside a tool so legitimately
            # slow tools get the higher staleness ceiling, mirroring the
            # sync-path heartbeat monitor.
            parts = []
            in_tool = False
            with _active_child_refs_lock:
                current_children = list(_active_child_refs)
            for _c in current_children:
                try:
                    _summary = _c.get_activity_summary()
                    _tool = _summary.get("current_tool")
                    parts.append(
                        (
                            _summary.get("api_call_count", 0),
                            _tool,
                            _summary.get("last_activity_ts"),
                        )
                    )
                    in_tool = in_tool or bool(_tool)
                except Exception:
                    parts.append(None)
            return tuple(parts), in_tool

        _goals = [t["goal"] for t in task_list]
        dispatch = dispatch_async_delegation_batch(
            goals=_goals,
            context=context,
            # Metadata for the completion block only; subagents inherit the
            toolsets=profile_toolsets,
            role=top_role,
            model=creds["model"],
            session_key=_session_key,
            origin_ui_session_id=_origin_ui_session_id,
            origin_session_id=_wake_sid,
            parent_session_id=_parent_session_id,
            runner=_batch_runner,
            interrupt_fn=_batch_interrupt,
            max_async_children=_get_max_async_children(),
            # Reuse the live-transcript directory's id (when created) so the
            # returned delegation_id matches cache/delegation/live/<id>/.
            delegation_id=live_deleg_id,
            progress_fn=_batch_progress,
        )

        if dispatch.get("status") == "dispatched":
            n = len(_goals)
            note = (
                "Subagent is running in the background. You and the user can "
                "keep working; its full result re-enters the conversation as a "
                "new message when it finishes. Do not wait or poll — just "
                "continue."
                if n == 1 else
                f"{n} subagents are running in parallel in the background. You "
                f"and the user can keep working; they wait on each other and "
                f"their consolidated results re-enter the conversation as a "
                f"single message once ALL of them finish. Do not wait or poll "
                f"— just continue."
            )
            payload = {
                "status": "dispatched",
                "mode": "background",
                "count": n,
                "delegation_id": dispatch["delegation_id"],
                "goals": _goals,
                "note": note,
            }
            _sids = [
                getattr(_c, "_subagent_id", None) for _c in _child_agents
            ]
            if any(isinstance(s, str) and s for s in _sids):
                payload["subagent_ids"] = _sids
                payload["control_hint"] = (
                    "While a child runs you can orchestrate it live with this "
                    "same tool: delegate_task(action='list') to see live "
                    "children, action='steer' with subagent_id + message to "
                    "redirect one, action='stop' with subagent_id to end one "
                    "early."
                )
            if live_paths:
                payload["live_transcripts"] = list(live_paths)
                payload["live_transcripts_hint"] = (
                    "Each subagent streams a human-readable transcript of its "
                    "operations to the file listed above (append-only, one per "
                    "task). Read or `tail -f` these paths at any time to watch "
                    "a child work while it runs."
                )
            return json.dumps(payload, ensure_ascii=False)

        # Pool at capacity / schedule failure: do not run inline, because that
        # would bypass the same concurrency limit that rejected this batch.
        logger.info(
            "delegate_task: async pool at capacity (%s); rejecting the new "
            "batch.",
            dispatch.get("error", "rejected"),
        )
        return _reject_unstarted_batch(batch, dispatch.get("error"))

    # ----- Synchronous path -----
    return json.dumps(
        _strip_internal_completion_metadata(_execute_and_aggregate()),
        ensure_ascii=False,
    )








# ---------------------------------------------------------------------------
# OpenAI Function-Calling Schema
# ---------------------------------------------------------------------------

# ── OpenAI function-calling schema ──────────────────────────────────────────

def _build_top_level_description() -> str:
    """delegate_task description: ONLY guidance stated nowhere else in the schema
    (limits live in the 'tasks' parameter description, rebuilt per get_definitions())."""
    try:
        orchestration_available = _get_max_spawn_depth() >= 2 and _get_orchestrator_enabled()
    except Exception:
        orchestration_available = False
    # Mention recursion only where it's actually available. send_message is deliberately not named (gateway-internal
    # vocabulary); model_tools session-filters the list to tools the session has.
    if orchestration_available:
        restrictions_rule = (
            "- Children cannot call clarify, memory, or cronjob.\n"
            f"- Children can themselves delegate while depth remains (max_spawn_depth={_get_max_spawn_depth()}); the "
            "runtime derives this from depth automatically.\n"
        )
    else:
        restrictions_rule = "- Children cannot call delegate_task, clarify, memory, or cronjob.\n"
    return _DESCRIPTION_HEAD + restrictions_rule + _DESCRIPTION_TAIL

_DESCRIPTION_HEAD = (
    "Spawn subagents in isolated contexts; each gets its own conversation, terminal session, and toolset, and only its "
    "final summary returns to you. Pass every task in `tasks` — one entry spawns one subagent, several run in parallel "
    "(limit in the tasks description).\n\n"
    "Runs in the background: dispatch returns immediately with live transcript paths, and the call's results re-enter "
    "the conversation as a new message when its subagents finish (one message per call by default; with "
    "delegation.independent_completions each ungrouped task / `group` returns on its own). Results are delivered only "
    "BETWEEN your turns: finish whatever does not depend on them, then give a one-line status and END YOUR TURN. Never "
    "wait or poll on transcripts, artifact files, or CI for a child. "
    "While children run, `action` (list/steer/stop) controls them live — steer when a transcript shows a "
    "child drifting.\n\n"
    "USE FOR: reasoning-heavy subtasks, work that would flood your context with intermediate data, or independent "
    "parallel workstreams.\n"
    "DO NOT USE FOR (use these instead):\n"
    "- Mechanical multi-step work with no reasoning needed -> execute_code\n"
    "- A single tool call -> call the tool directly\n"
    "- Tasks needing user interaction -> subagents cannot ask questions\n"
    "- Durable work that must survive this session -> cronjob or terminal(background=True, notify=True); /stop, /new, "
    "or process exit discards running subagents.\n\n"
    "RULES:\n"
    "- Children know nothing of this conversation: pass everything needed via 'context', including any required "
    "output language, tone, or style (e.g. \"respond in Chinese\").\n"
    "- Child summaries are SELF-REPORTS, not verified facts: a child claiming \"uploaded successfully\" or "
    "\"file written\" may be wrong. For external side effects (uploads, remote writes, publishing), require a "
    "verifiable handle (URL, ID, absolute path) and verify it yourself before telling the user the operation "
    "succeeded.\n"
)
_DESCRIPTION_TAIL = (
    "- Children inherit the parent model unless pinned via delegation.provider / delegation.model in config.yaml."
)

def _build_tasks_param_description() -> str:
    """Compose the 'tasks' parameter description with current concurrency limit."""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"The task(s), with up to {max_children} running concurrently (set "
        "via delegation.max_concurrent_children); larger batches queue "
        "remaining tasks until a worker is free. Each entry spawns one "
        "subagent with isolated context and terminal session; a single task "
        "is a one-entry array. Required when spawning. For "
        "tool_profile=backlinkhub, each task must cover exactly one site; "
        "split multi-site work into one task per site so independent "
        "completions remain attributable."
    )

def _build_dynamic_schema_overrides() -> dict:
    """Per-call schema overrides (ToolEntry.dynamic_schema_overrides): every
    get_definitions() pass rewrites the descriptions to the user's actual limits."""
    overrides_params = {**DELEGATE_TASK_SCHEMA["parameters"]}
    # Copy properties so the static schema dict is never mutated.
    overrides_params["properties"] = {k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()}
    overrides_params["properties"]["tasks"]["description"] = _build_tasks_param_description()
    if "role" in overrides_params["properties"]:
        overrides_params["properties"]["role"]["description"] = _build_role_param_description()
    profile_names = _configured_tool_profile_names()
    profile_schema = dict(overrides_params["properties"]["tool_profile"])
    if profile_names:
        profile_schema["enum"] = profile_names
        profile_schema["description"] = (
            "Optional operator-defined child capability profile. Available: "
            + ", ".join(profile_names)
            + ". Profiles can only narrow the parent's tools; raw toolsets "
            "are never model-controlled."
        )
    else:
        profile_schema["description"] = (
            "Optional operator-defined child capability profile. No profiles "
            "are currently configured in delegation.tool_profiles."
        )
    overrides_params["properties"]["tool_profile"] = profile_schema

    return {"description": _build_top_level_description(), "parameters": overrides_params}

def _p(type_: str, description: str, **extra) -> dict:
    return {"type": type_, **extra, "description": description}

DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # description / tasks.description are placeholders: the real text is built per get_definitions() call by
    # _build_dynamic_schema_overrides() so the model sees the user's actual max_concurrent_children / max_spawn_depth.
    # Lazy (not at import) so cli.CLI_CONFIG isn't forced to load before the test conftest redirects HERMES_HOME.
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            # The handler also accepts the legacy single-goal shape (top-level `goal`/`context`/`output_schema`),
            # wrapped into a one-entry batch at dispatch, and a per-task `role` (legacy, ignored: capability is
            # depth-derived). Both unadvertised on purpose (old transcripts only); do not re-add. No maxItems — the
            # runtime limit (delegation.max_concurrent_children) is enforced with a clear error in delegate_task().
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": _p(
                            "string",
                            "What this subagent should accomplish. Be specific and self-contained — it knows "
                            "nothing about your conversation history.",
                        ),
                        "context": _p(
                            "string",
                            "Background THIS child needs: file paths, error messages, constraints. Each child "
                            "sees only its own context — repeat shared background in every task that needs it.",
                        ),
                        "output_schema": _p(
                            "object",
                            "Optional JSON Schema this child's final answer must validate against (told to the "
                            "child up front; parent validates with one bounded correction retry; result gains "
                            "schema_valid, plus schema_errors on failure). Keep it forgiving — require only "
                            "fields you will read.",
                        ),
                        "group": _p(
                            "string",
                            "Optional result-delivery bucket within this call (only when delegation.independent_completions "
                            "is enabled; otherwise the whole call returns as one message). Tasks sharing a group return "
                            "together in ONE message; ungrouped tasks return individually as each finishes. This does not "
                            "order execution; if B needs A's output, dispatch B after A returns.",
                        ),
                    },
                    "required": ["goal"],
                },
                "description": "(rebuilt at get_definitions() time)",
            },
            "tool_profile": {
                "type": "string",
                "description": (
                    "Operator-defined child capability profile. The available "
                    "names are rebuilt from delegation.tool_profiles."
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "DEPRECATED / IGNORED. Top-level single and batch "
                    "delegations run in the background automatically — you do "
                    "not need to (and cannot) opt in or out. A single result or "
                    "consolidated batch result re-enters the conversation when "
                    "the work finishes; just continue working in the meantime. "
                    "Setting this has no effect; the parameter remains only for "
                    "backward compatibility."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["spawn", "list", "steer", "stop"],
                "description": (
                    "Default 'spawn' (omit for normal delegation). Live "
                    "orchestration of running subagents: 'list' shows this "
                    "conversation's live children (ids, goals, status, "
                    "transcript paths); 'steer' queues course-correction text "
                    "into one child (requires subagent_id + message) without "
                    "stopping it; 'stop' ends one child early (requires "
                    "subagent_id) — its partial result still returns as a "
                    "completion message. Control actions return immediately; "
                    "goal/tasks are ignored when action is not 'spawn'."
                ),
            },
            "subagent_id": {
                "type": "string",
                "description": (
                    "Target for action='steer'/'stop'. Ids are returned in the "
                    "spawn dispatch response (subagent_ids) and by "
                    "action='list'."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "For action='steer': the course correction. Be directive "
                    "and specific — the child sees it appended to its next "
                    "tool result mid-run (e.g. \"Stop exploring X; focus on Y "
                    "and return early results\")."
                ),
            },
            # `background` (bool) is also accepted — DEPRECATED, ignored: top-level
            # delegations always run in the background. Unadvertised; do not re-add.
            "action": _p(
                "string",
                "Default 'spawn'. Live control of running children: "
                "'list' = ids/goals/status/transcripts; 'steer' = queue "
                "course-correction text into one child (subagent_id + "
                "message) without stopping it; 'stop' = end one child "
                "early (subagent_id; partial result still returns). "
                "Control actions return immediately; goal/tasks are ignored unless spawning.",
                enum=["spawn", "list", "steer", "stop"],
            ),
            "subagent_id": _p("string", "Target for action='steer'/'stop' (ids from the spawn response or action='list')."),
            "message": _p(
                "string",
                "For action='steer': the course correction, appended to "
                "the child's next tool result mid-run. Be directive and specific.",
            ),
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

def _model_background_value(args: dict, parent_agent=None) -> bool:
    """Background flag for the MODEL-facing dispatch path (registry fallback). Top-level delegations always run in the
    background — the model does not choose — for single tasks and fan-out batches alike (one async unit, one
    consolidated result); an orchestrator subagent (depth > 0) is the exception since it needs its workers' results
    within its own turn. The live path is ``run_agent._dispatch_delegate_task``; this mirrors it for the rare case
    the intercept is bypassed. Direct Python callers keep the synchronous default."""
    return not getattr(parent_agent, "_delegate_depth", 0) > 0

_MODEL_HIDDEN_TASK_FIELDS = {"acp_command", "acp_args"}

def _strip_model_hidden_task_fields(tasks: Any) -> Any:
    """Drop trusted-config-only task fields from model-supplied tasks (same list object back when nothing changed)."""
    if not isinstance(tasks, list) or not any(isinstance(t, dict) and _MODEL_HIDDEN_TASK_FIELDS & t.keys() for t in tasks):
        return tasks
    return [{k: v for k, v in t.items() if k not in _MODEL_HIDDEN_TASK_FIELDS} if isinstance(t, dict) else t for t in tasks]


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"),
        context=args.get("context"),
        tasks=_strip_model_hidden_task_fields(args.get("tasks")),
        max_iterations=args.get("max_iterations"),
        role=args.get("role"),
        tool_profile=args.get("tool_profile"),
        background=_model_background_value(args, kw.get("parent_agent")),
        output_schema=args.get("output_schema"),
        _auto_continue=args.get("_auto_continue"),
        action=args.get("action"),
        subagent_id=args.get("subagent_id"),
        message=args.get("message"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from concurrent.futures import TimeoutError as FuturesTimeoutError  # noqa: F401,E402
import contextvars  # noqa: F401,E402
import enum  # noqa: F401,E402
import json  # noqa: F401,E402
import os  # noqa: F401,E402
import re  # noqa: F401,E402
import threading  # noqa: F401,E402
from urllib.parse import urlsplit  # noqa: F401,E402
from urllib.parse import urlunsplit  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    'DEFAULT_CHILD_TIMEOUT': ('tools.delegate_tool_config', 'DEFAULT_CHILD_TIMEOUT'),
    'DEFAULT_MAX_SUMMARY_CHARS': ('tools.delegate_tool_results', 'DEFAULT_MAX_SUMMARY_CHARS'),
    'DEFAULT_TOOLSETS': ('tools.delegate_tool_toolsets', 'DEFAULT_TOOLSETS'),
    'MAX_DEPTH': ('tools.delegate_tool_config', 'MAX_DEPTH'),
    'TOOLSETS': ('toolsets', 'TOOLSETS'),
    'base_url_hostname': ('utils', 'base_url_hostname'),
    'file_state': ('tools', 'file_state'),
    'request_hard_interrupt': ('agent.interrupt_compat', 'request_hard_interrupt'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
