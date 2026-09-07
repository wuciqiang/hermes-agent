"""Structured-output schema helpers for delegate_task (T1-24).

Optional per-task ``output_schema`` (a JSON Schema object): the child is
told about the contract via an OUTPUT CONTRACT block appended to its
context, the parent validates the child's final answer with jsonschema,
and on failure sends exactly ONE bounded retry turn carrying the
validation errors verbatim (per llm-structured-output-schema-design:
max 1 retry, exact errors, no schema re-paste).

Pattern from: github/copilot-cli ctx.agent(prompt, {schema}) — PATTERN
ONLY, zero code/prompt text copied (proprietary).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Exactly one retry turn — bounded by design. More retries make frontier
# models drop fields that were right the first time.
MAX_SCHEMA_RETRIES = 1

_CONTRACT_HEADER = "OUTPUT CONTRACT (machine-validated)"

_CONTINUATION_COUNT_FIELDS = (
    "published",
    "pending",
    "attempted_unconfirmed",
    "failed_retryable",
    "failed_final",
    "remaining",
)

# A normal ``completed`` child result is terminal unless it carries one of
# these explicit, machine-readable segment boundaries.  In particular, do not
# infer a boundary merely from ``remaining > 0``: a worker can finish normally
# after a genuine terminal outcome (or after losing its browser control).
_NATIVE_RESUMABLE_EXIT_REASON = "max_iterations"
_EXPLICIT_SEGMENT_STOP_REASON = "segment_iteration_boundary"
_SAFE_BOUND_CANDIDATE_STOP_REASON = "candidate_available"
_KNOWN_EARLY_STOP_REASONS = frozenset(
    {
        "stopped_without_native_termination_after_current_execution_segment",
        # Stable boundary labels emitted by earlier BacklinkHub worker
        # contracts.  Keep this list finite; never classify arbitrary prose as
        # a resumable boundary.
        "model_iteration_boundary",
        "continuation_required_model_iteration_boundary",
        "worker_execution_boundary_before_target",
        "execution_segment_boundary_no_candidate_left_bound_after_final_immediate_record",
        "execution_context_ended_before_queue_exhaustion",
        "serial_continuation_boundary",
        "continuation_required",
        "agent_execution_budget_reached",
        "maximum_tool_calling_iterations_reached",
        "hermes_tool_call_iteration_limit_reached",
        "worker_execution_boundary",
    }
)

# These are explicit operator/system boundaries.  Reasons are matched exactly
# (apart from the small ownership-prefix compatibility cases below), so a
# transient platform ``network_timeout`` or a candidate ``failed_final`` does
# not accidentally terminate the whole site round.
_NON_RESUMABLE_STOP_REASONS = frozenset(
    {
        "user_stop",
        "user_requested_stop",
        "interrupted",
        "timeout",
        "handoff",
        "ownership_user",
        "user_control",
        "control_confirmation",
        "controller_error",
        "backlinkhub_unavailable",
        "external_side_effect_unknown",
        "agent_delegated_to_user",
        "ownership_agent_delegated_to_user",
        "system_error",
        "worker_error",
        "user_is_controlling",
        "ego_user_control",
        "task_space_user_controlled",
        "ego_task_space_user_controlled",
        "user_controlled_task_space",
        "true_user_control",
        "user_takeover",
        "real_user_takeover",
        "manual_handoff",
    }
)

_PRESERVED_EGO_CLEANUP_STATES = frozenset(
    {
        "preserved_for_continuation",
        "preserved",
        "open",
        "active",
        "kept",
        "not_closed",
        "reused",
        "preserved_for_serial_continuation",
    }
)

_EGO_CLEANUP_ALIASES = {
    # BacklinkHub worker output observed before the cleanup-state contract was
    # narrowed. The space was deliberately left open for the next segment.
    "not_cleaned_iteration_boundary": "preserved_for_continuation",
}


def _normalize_reason(value: Any) -> str:
    """Normalize a machine reason without interpreting free-form prose."""

    text = str(value or "")
    # Make the official Ego camelCase ownership value comparable to the
    # snake_case values emitted by the BacklinkHub schema.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()


def _is_non_resumable_reason(value: Any) -> bool:
    normalized = _normalize_reason(value)
    if not normalized:
        return False
    if normalized in _NON_RESUMABLE_STOP_REASONS:
        return True
    # Ego has emitted explanatory suffixes alongside these ownership labels.
    # Only allow suffix matching for the explicit control families; never do a
    # general substring search across arbitrary stop text.
    return normalized.startswith(
        (
            "task_space_user_controlled_",
            "ego_task_space_user_controlled_",
            "user_is_controlling_",
            "user_controlled_task_space_",
            "user_takeover_",
            "real_user_takeover_",
            "manual_handoff_",
        )
    )


def _valid_ego_space_id(value: Any) -> bool:
    """Ego task-space ids are positive integers; bool is not an id."""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_nonnegative_int(value: Any) -> bool:
    """Return whether a progress value is an actual non-negative integer."""

    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _continuation_boundary_kind(
    data: Dict[str, Any], *, exit_reason: Any = None
) -> Optional[str]:
    """Return the exact boundary kind represented by a normalized payload."""

    effective_exit = _normalize_reason(
        exit_reason if exit_reason is not None else data.get("exit_reason")
    )
    stop_reason = _normalize_reason(data.get("stop_reason"))
    reported_reason = _normalize_reason(data.get("reported_stop_reason"))

    explicit_boundary = (
        data.get("segment_iteration_boundary") is True
        and stop_reason == _EXPLICIT_SEGMENT_STOP_REASON
    )
    legacy_boundary = (
        data.get("segment_iteration_boundary") is True
        and stop_reason == "worker_returned_early"
        and reported_reason == _EXPLICIT_SEGMENT_STOP_REASON
    )
    known_early_boundary = (
        stop_reason in _KNOWN_EARLY_STOP_REASONS
        or reported_reason in _KNOWN_EARLY_STOP_REASONS
        or effective_exit in _KNOWN_EARLY_STOP_REASONS
    )
    safe_bound_candidate = (
        effective_exit == "completed"
        and stop_reason == _SAFE_BOUND_CANDIDATE_STOP_REASON
        and data.get("segment_iteration_boundary") is False
        and data.get("candidate_bound") is True
        and data.get("candidate_external_side_effect") == "none"
    )

    if explicit_boundary:
        return "explicit"
    if legacy_boundary:
        return "legacy"
    if known_early_boundary:
        return "known_early"
    if safe_bound_candidate:
        # ``advance`` can bind the next candidate immediately before a worker
        # returns.  That is a safe serial handoff only while the candidate is
        # untouched; the caller still enforces remaining/queue/cleanup facts
        # and the outer continuation loop stops repeated no-progress states.
        return "bound_candidate"

    if effective_exit == _NATIVE_RESUMABLE_EXIT_REASON or (
        not effective_exit and stop_reason == _NATIVE_RESUMABLE_EXIT_REASON
    ):
        # Check explicit/legacy boundaries first: a completed safe boundary is
        # normalized to ``max_iterations`` before its private metadata reaches
        # the host continuation loop. Reject only an otherwise-unexplained
        # segment marker paired with a native max-iteration result.
        if data.get("segment_iteration_boundary") is True:
            return None
        return "native"
    return None


def _structured_payload(value: Any) -> Dict[str, Any]:
    """Parse a validated completion object without interpreting prose."""

    if isinstance(value, dict):
        return normalize_completion_payload(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    raw = value.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            raw = "\n".join(lines[1:-1]).strip()
    candidate = extract_json_candidate(raw)
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return normalize_completion_payload(parsed) if isinstance(parsed, dict) else {}


def completion_can_continue(
    payload: Any,
    *,
    exit_reason: Any = None,
    schema_valid: Any = None,
) -> bool:
    """Return whether an incomplete structured round is safe to resume.

    This is deliberately a narrow, fact-only predicate.  It never treats a
    natural-language request to continue as authorization and never resumes a
    candidate after a confirmed or unknown external side effect.
    """

    data = _structured_payload(payload)
    if not data:
        return False
    # Continuation is a host-owned control decision.  Only a result that
    # passed the requested JSON Schema may drive it; prose or an unvalidated
    # legacy payload must never create another browser worker.
    trusted_schema = data.get("schema_valid") if schema_valid is None else schema_valid
    if trusted_schema is not True:
        return False
    if data.get("target_reached") is not False:
        return False
    if data.get("queue_exhausted") is not False:
        return False
    if not _valid_nonnegative_int(data.get("remaining")):
        return False
    if data["remaining"] <= 0:
        return False
    if not _valid_ego_space_id(data.get("target")):
        return False
    if any(
        not _valid_nonnegative_int(data.get(field))
        for field in _CONTINUATION_COUNT_FIELDS
    ):
        return False
    if data.get("candidate_bound") not in (False, True):
        return False
    if (
        data.get("candidate_bound") is True
        and data.get("candidate_external_side_effect") != "none"
    ):
        return False

    # Check both the current and legacy/audit reason.  The latter can carry a
    # user-control marker even when a compatibility normalizer changed the
    # active stop reason to ``worker_returned_early``.
    if _is_non_resumable_reason(data.get("stop_reason")) or _is_non_resumable_reason(
        data.get("reported_stop_reason")
    ):
        return False

    boundary_kind = _continuation_boundary_kind(data, exit_reason=exit_reason)
    if boundary_kind is None:
        # A plain ``completed`` (or an absent/unknown exit reason) is terminal.
        return False

    cleanup = _normalize_reason(data.get("ego_cleanup"))
    space_id = data.get("ego_task_space_id")
    if cleanup == "control_confirmation_required":
        return False
    if cleanup in _PRESERVED_EGO_CLEANUP_STATES:
        if not _valid_ego_space_id(space_id):
            return False
    elif cleanup in {"closed", "not_created"}:
        # Closing an otherwise safe segment loses browser state, but it does
        # not make the BacklinkHub round unsafe.  The next child can restore
        # the bound candidate (or claim the next one) and create a replacement
        # Ego space.  Preserve a supplied old id only as an audit/resume hint.
        if space_id is not None and not _valid_ego_space_id(space_id):
            return False
    else:
        return False

    return True


def continuation_progress_fingerprint(payload: Any) -> Optional[Tuple[Any, ...]]:
    """Return the progress facts used to detect a no-progress continuation.

    Stop/cleanup labels are intentionally excluded: those can change between
    execution segments without any BacklinkHub work being recorded.  A stable
    fingerprint therefore means that no candidate outcome or queue state
    changed, which is the only condition under which the host should stop a
    would-be endless continuation loop.
    """

    data = _structured_payload(payload)
    if not data:
        return None
    fields = (
        "published",
        "pending",
        "attempted_unconfirmed",
        "failed_retryable",
        "failed_final",
        "remaining",
        "queue_exhausted",
        "target_reached",
        "candidate_bound",
        "candidate_external_side_effect",
    )
    if any(field not in data for field in fields):
        return None
    return tuple(data[field] for field in fields)


def normalize_completion_exit_reason(
    payload: Any,
    *,
    schema_valid: Any,
    exit_reason: Any,
) -> str:
    """Promote only an explicit safe boundary to Hermes' native boundary.

    ``completed`` is not evidence that a round is unfinished or resumable.
    The compatibility mapping is limited to the exact segment marker emitted
    by the BacklinkHub contract and the one observed defensive early-return
    reason.  User handoff, errors, and uncertain side effects retain their
    original exit reason.
    """

    reason = str(exit_reason or "")
    if reason != "completed" or schema_valid is not True:
        return reason
    return (
        _NATIVE_RESUMABLE_EXIT_REASON
        if completion_can_continue(
            payload,
            exit_reason=reason,
            schema_valid=schema_valid,
        )
        else reason
    )


def normalize_completion_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the previous structured-worker result shape.

    Detached tasks can finish after their owning session has loaded an older
    output schema.  Keep that already-validated result usable across a skill
    hot reload: flatten the former ``progress`` counters, translate the former
    segment marker, and make the no-bound-candidate side-effect state explicit.
    The legacy stop reason is retained separately for auditability.
    """
    normalized = dict(payload)
    cleanup = _normalize_reason(normalized.get("ego_cleanup"))
    if cleanup in _EGO_CLEANUP_ALIASES:
        normalized["ego_cleanup"] = _EGO_CLEANUP_ALIASES[cleanup]

    progress = payload.get("progress")
    if isinstance(progress, dict):
        for key in _CONTINUATION_COUNT_FIELDS:
            if key not in normalized and key in progress:
                normalized[key] = progress[key]

    legacy_boundary = payload.get("segment_boundary")
    if (
        "segment_iteration_boundary" not in normalized
        and isinstance(legacy_boundary, bool)
    ):
        normalized["segment_iteration_boundary"] = legacy_boundary

    # Skill 3.17 used ``segment_boundary`` but its parent continuation rule
    # recognized ``worker_returned_early``.  A worker constrained by that old
    # schema can otherwise be schema-valid yet impossible for either the old
    # or new parent to resume.
    if (
        legacy_boundary is True
        and _normalize_reason(normalized.get("stop_reason"))
        == _EXPLICIT_SEGMENT_STOP_REASON
    ):
        normalized["reported_stop_reason"] = "segment_iteration_boundary"
        normalized["stop_reason"] = "worker_returned_early"

    if normalized.get("candidate_bound") is False:
        # This field describes only an unrecorded, currently bound candidate.
        # Once record+advance has released that candidate, a worker sometimes
        # reports the historical submit click here.  It cannot make the next
        # candidate unsafe, so canonicalize the logically impossible pair.
        normalized["candidate_external_side_effect"] = "none"
    elif (
        normalized.get("candidate_bound") is True
        and "candidate_external_side_effect" not in normalized
    ):
        normalized["candidate_external_side_effect"] = "unknown"
    return normalized


def coerce_output_schema(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a model/caller-supplied output_schema value.

    Returns ``(schema, None)`` when usable, ``(None, error)`` when not.
    ``None`` input passes through as ``(None, None)`` (no schema requested).
    """
    if raw is None:
        return None, None
    if isinstance(raw, str):
        # Models sometimes double-encode the schema as a JSON string.
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None, "output_schema must be a JSON Schema object, got a non-JSON string."
        if not isinstance(parsed, dict):
            return None, "output_schema must be a JSON Schema object."
        raw = parsed
    if not isinstance(raw, dict):
        return None, (
            f"output_schema must be a JSON Schema object, got {type(raw).__name__}."
        )
    try:
        from jsonschema.validators import validator_for  # type: ignore[import-untyped]

        validator_for(raw).check_schema(raw)
    except ImportError:
        # jsonschema is a hard dependency in practice; degrade to accepting
        # the dict as-is so delegation still works without it.
        logger.debug("jsonschema unavailable; skipping output_schema meta-validation")
    except Exception as exc:
        return None, f"output_schema is not a valid JSON Schema: {exc}"
    return raw, None


def append_output_contract(context: Optional[str], schema: Dict[str, Any]) -> str:
    """Append the explicit output contract block to a child's context."""
    try:
        schema_text = json.dumps(schema, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        schema_text = str(schema)
    block = (
        f"{_CONTRACT_HEADER}:\n"
        "Your FINAL response must be a single JSON object that validates "
        "against this JSON Schema. No prose before or after the JSON; a "
        "```json code fence is acceptable but not required.\n"
        f"{schema_text}"
    )
    base = (context or "").rstrip()
    return f"{base}\n\n{block}" if base else block


def extract_json_candidate(text: str) -> str:
    """Best-effort extraction of a JSON payload from model output.

    Strips markdown code fences and leading/trailing prose around the
    outermost ``{...}`` / ``[...]`` span. Returns the (possibly unchanged)
    candidate string; parsing errors are reported by validate_output.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[: -3]
        raw = raw.strip()
        if raw.lower().startswith("json\n"):
            raw = raw.split("\n", 1)[1]
    for opener, closer in (("{", "}"), ("[", "]")):
        if raw.startswith(opener):
            return raw
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            return raw[start : end + 1]
    return raw


def validate_output(
    text: str, schema: Dict[str, Any]
) -> Tuple[bool, List[str]]:
    """Validate a child's final answer against ``schema``.

    Returns ``(True, [])`` on success or ``(False, errors)`` where errors
    are human-readable strings suitable for the retry turn.
    """
    candidate = extract_json_candidate(text or "")
    if not candidate.strip():
        return False, ["Response was empty — expected a JSON object matching the schema."]
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError) as exc:
        return False, [f"Response is not valid JSON: {exc}"]
    try:
        from jsonschema.validators import validator_for  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("jsonschema unavailable; accepting parsed JSON without validation")
        return True, []
    validator = validator_for(schema)(schema)
    errors = sorted(validator.iter_errors(parsed), key=lambda e: list(e.absolute_path))
    if not errors:
        return True, []
    rendered: List[str] = []
    for err in errors[:10]:  # bound error volume for the retry prompt
        path = "$" + "".join(
            f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path
        )
        rendered.append(f"{path}: {err.message}")
    return False, rendered


def build_retry_message(errors: List[str]) -> str:
    """Build the single bounded retry turn sent to the child.

    Carries the validation errors verbatim; deliberately does NOT
    re-paste the schema (the child already has it in its context).
    """
    error_block = "\n".join(f"- {e}" for e in errors)
    return (
        "Your previous final response was rejected by the output contract "
        "validator. Validation errors:\n"
        f"{error_block}\n\n"
        "Reply with ONLY the corrected JSON object matching the OUTPUT "
        "CONTRACT schema from your task context. No prose, no explanations."
    )
