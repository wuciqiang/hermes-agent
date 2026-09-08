"""T1-24: structured-output schema on delegate_task.

Per-task ``output_schema`` (JSON Schema object): the child receives the
schema as an explicit output contract, the parent validates the child's
final answer with jsonschema, and on failure sends exactly ONE bounded
retry turn carrying the validation errors. Result entries gain
``schema_valid`` / ``schema_errors`` / ``schema_retries`` ONLY when a
schema was requested — schema-less calls keep a byte-identical result
shape (wire-shape pinning).

Pattern from: github/copilot-cli ctx.agent(prompt, {schema}) — PATTERN
ONLY, zero code/prompt text copied (proprietary).
"""

import json
import threading
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _cleanup_empty_agent_ego_space,
    _run_single_child,
    delegate_task,
)
from tools.delegation_output_schema import (
    append_output_contract,
    build_retry_message,
    completion_can_continue,
    coerce_output_schema,
    continuation_progress_fingerprint,
    failed_segment_can_continue,
    normalize_completion_exit_reason,
    normalize_completion_payload,
    validate_output,
)

ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "zip": {"type": "string"},
    },
    "required": ["city"],
}

ROUND_SCHEMA = {
    "type": "object",
    "required": [
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
        "ego_task_space_id",
        "ego_cleanup",
        "segment_iteration_boundary",
        "candidate_bound",
        "candidate_external_side_effect",
    ],
    "properties": {
        "site_id": {"type": "string"},
        "run_id": {"type": "string"},
        "target": {"type": "integer", "minimum": 1},
        "published": {"type": "integer", "minimum": 0},
        "pending": {"type": "integer", "minimum": 0},
        "attempted_unconfirmed": {"type": "integer", "minimum": 0},
        "failed_retryable": {"type": "integer", "minimum": 0},
        "failed_final": {"type": "integer", "minimum": 0},
        "remaining": {"type": "integer", "minimum": 0},
        "queue_exhausted": {"type": "boolean"},
        "target_reached": {"type": "boolean"},
        "stop_reason": {"type": "string"},
        "ego_task_space_id": {
            "anyOf": [
                {"type": "integer", "minimum": 1},
                {"type": "null"},
            ]
        },
        "ego_cleanup": {"type": "string"},
        "segment_iteration_boundary": {"type": "boolean"},
        "candidate_bound": {"type": "boolean"},
        "candidate_external_side_effect": {
            "type": "string",
            "enum": ["none", "confirmed", "unknown"],
        },
    },
    "additionalProperties": True,
}


def _round_payload(**overrides):
    payload = {
        "site_id": "site_thesitemath",
        "run_id": "round_test",
        "target": 6,
        "published": 0,
        "pending": 1,
        "attempted_unconfirmed": 0,
        "failed_retryable": 2,
        "failed_final": 3,
        "remaining": 5,
        "queue_exhausted": False,
        "target_reached": False,
        "stop_reason": "segment_iteration_boundary",
        "ego_task_space_id": 7,
        "ego_cleanup": "preserved_for_continuation",
        "segment_iteration_boundary": True,
        "candidate_bound": False,
        "candidate_external_side_effect": "none",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Helper-module unit tests
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_valid_json_matching_schema(self):
        ok, errors = validate_output('{"city": "Berlin"}', ADDRESS_SCHEMA)
        assert ok is True
        assert errors == []

    def test_json_violating_schema_reports_errors(self):
        ok, errors = validate_output('{"zip": "10115"}', ADDRESS_SCHEMA)
        assert ok is False
        assert errors
        assert any("city" in e for e in errors)

    def test_non_json_text_reports_parse_error(self):
        ok, errors = validate_output("I could not produce JSON, sorry.", ADDRESS_SCHEMA)
        assert ok is False
        assert errors

    def test_code_fenced_json_is_accepted(self):
        text = '```json\n{"city": "Oslo"}\n```'
        ok, errors = validate_output(text, ADDRESS_SCHEMA)
        assert ok is True
        assert errors == []

    def test_json_embedded_in_prose_is_extracted(self):
        text = 'Here is the result:\n{"city": "Lima"}\nHope that helps!'
        ok, _ = validate_output(text, ADDRESS_SCHEMA)
        assert ok is True

    def test_empty_text_is_invalid(self):
        ok, errors = validate_output("", ADDRESS_SCHEMA)
        assert ok is False
        assert errors


class TestCoerceOutputSchema:
    def test_valid_schema_passes(self):
        schema, err = coerce_output_schema(ADDRESS_SCHEMA)
        assert schema == ADDRESS_SCHEMA
        assert err is None

    def test_none_passes_through(self):
        schema, err = coerce_output_schema(None)
        assert schema is None
        assert err is None

    def test_non_dict_is_rejected(self):
        schema, err = coerce_output_schema("not a schema")
        assert schema is None
        assert err

    def test_invalid_json_schema_is_rejected(self):
        schema, err = coerce_output_schema({"type": 42})
        assert schema is None
        assert err


class TestPromptPlumbing:
    def test_contract_block_carries_schema(self):
        out = append_output_contract("base context", ADDRESS_SCHEMA)
        assert "base context" in out
        assert "OUTPUT CONTRACT" in out
        assert '"city"' in out

    def test_contract_block_without_prior_context(self):
        out = append_output_contract(None, ADDRESS_SCHEMA)
        assert "OUTPUT CONTRACT" in out

    def test_retry_message_carries_verbatim_errors(self):
        msg = build_retry_message(["'city' is a required property"])
        assert "'city' is a required property" in msg
        assert "JSON" in msg


class TestContinuationBoundary:
    @staticmethod
    def _unfinished(**overrides):
        payload = {
            "schema_valid": True,
            "site_id": "site_one",
            "run_id": "round_one",
            "target": 3,
            "published": 1,
            "pending": 0,
            "attempted_unconfirmed": 0,
            "failed_retryable": 2,
            "failed_final": 0,
            "remaining": 2,
            "queue_exhausted": False,
            "target_reached": False,
            "stop_reason": "max_iterations",
            "ego_task_space_id": 7,
            "ego_cleanup": "preserved_for_continuation",
            "segment_iteration_boundary": False,
            "candidate_bound": False,
            "candidate_external_side_effect": "none",
        }
        payload.update(overrides)
        return payload

    def test_native_max_iterations_result_can_continue(self):
        payload = self._unfinished()

        assert completion_can_continue(payload) is True
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="max_iterations",
        ) == "max_iterations"

    def test_plain_completed_result_does_not_continue(self):
        payload = self._unfinished(
            stop_reason="completed",
            ego_cleanup="closed",
        )

        assert completion_can_continue(payload, exit_reason="completed") is False
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="completed",
        ) == "completed"

    def test_completed_bound_candidate_without_side_effect_can_continue(self):
        """A claimed but untouched next candidate is a safe serial handoff."""

        payload = self._unfinished(
            stop_reason="candidate_available",
            segment_iteration_boundary=False,
            candidate_bound=True,
            candidate_external_side_effect="none",
        )

        assert completion_can_continue(payload, exit_reason="completed") is True
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="completed",
        ) == "max_iterations"

    def test_completed_candidate_handoff_requires_bound_untouched_candidate(self):
        for candidate_bound, side_effect in (
            (False, "none"),
            (True, "confirmed"),
            (True, "unknown"),
        ):
            payload = self._unfinished(
                stop_reason="candidate_available",
                segment_iteration_boundary=False,
                candidate_bound=candidate_bound,
                candidate_external_side_effect=side_effect,
            )

            assert completion_can_continue(payload, exit_reason="completed") is False

    def test_explicit_segment_boundary_can_continue(self):
        payload = self._unfinished(
            stop_reason="segment_iteration_boundary",
            segment_iteration_boundary=True,
        )

        assert completion_can_continue(payload, exit_reason="completed") is True
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="completed",
        ) == "max_iterations"
        assert completion_can_continue(payload, exit_reason="max_iterations") is True

    def test_observed_iteration_cleanup_alias_is_safe_to_continue(self):
        payload = self._unfinished(
            stop_reason="segment_iteration_boundary",
            ego_cleanup="not_cleaned_iteration_boundary",
            segment_iteration_boundary=True,
        )

        normalized = normalize_completion_payload(payload)
        assert normalized["ego_cleanup"] == "preserved_for_continuation"
        assert completion_can_continue(payload, exit_reason="completed") is True
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="completed",
        ) == "max_iterations"

    def test_known_defensive_early_return_can_continue_with_closed_space(self):
        payload = self._unfinished(
            stop_reason=(
                "stopped_without_native_termination_after_current_execution_segment"
            ),
            ego_cleanup="closed",
        )

        assert completion_can_continue(payload, exit_reason="completed") is True
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="completed",
        ) == "max_iterations"

    def test_serial_continuation_preserves_reusable_space(self):
        payload = self._unfinished(
            stop_reason="serial_continuation_boundary",
            ego_cleanup="preserved_for_serial_continuation",
        )

        assert completion_can_continue(payload) is True

    def test_reported_known_boundary_can_continue(self):
        payload = self._unfinished(
            stop_reason="worker_returned_early",
            reported_stop_reason="worker_execution_boundary_before_target",
            ego_cleanup="preserved_for_continuation",
        )

        assert completion_can_continue(payload, exit_reason="completed") is True

    def test_network_timeout_does_not_cancel_native_boundary(self):
        """A candidate network failure must not mask a native segment boundary."""
        payload = self._unfinished(
            stop_reason="network_timeout",
            ego_cleanup="preserved_for_continuation",
        )

        assert completion_can_continue(payload, exit_reason="max_iterations") is True

    def test_true_user_control_does_not_continue(self):
        payload = self._unfinished(
            stop_reason="task_space_user_controlled",
            ego_cleanup="closed",
        )

        assert completion_can_continue(payload) is False
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="completed",
        ) == "completed"

    def test_all_known_user_control_variants_do_not_continue(self):
        for reason in (
            "user_is_controlling",
            "ego_user_control",
            "user_takeover",
            "real_user_takeover_after_navigation",
            "manual_handoff",
            "ownership=user",
            "ownership=agentDelegatedToUser",
        ):
            payload = self._unfinished(stop_reason=reason, ego_cleanup="closed")
            assert completion_can_continue(payload, exit_reason="completed") is False

    def test_unknown_external_side_effect_does_not_continue(self):
        for side_effect in ("confirmed", "unknown"):
            payload = self._unfinished(
                candidate_bound=True,
                candidate_external_side_effect=side_effect,
            )

            assert completion_can_continue(payload) is False

    def test_latest_real_boundary_ignores_historical_effect_after_record(self):
        payload = self._unfinished(
            site_id="site_thesitemath",
            run_id="round_20260903_142813_c7c79100",
            target=6,
            published=0,
            pending=0,
            failed_retryable=8,
            failed_final=1,
            remaining=6,
            stop_reason="segment_iteration_boundary",
            ego_task_space_id=8,
            ego_cleanup="closed",
            segment_iteration_boundary=True,
            candidate_bound=False,
            candidate_external_side_effect="confirmed",
        )

        normalized = normalize_completion_payload(payload)

        assert normalized["candidate_external_side_effect"] == "none"
        assert completion_can_continue(payload, exit_reason="completed") is True
        assert normalize_completion_exit_reason(
            payload,
            schema_valid=True,
            exit_reason="completed",
        ) == "max_iterations"

    def test_already_terminal_result_does_not_continue(self):
        payload = self._unfinished(target_reached=True, remaining=0)

        assert completion_can_continue(payload) is False

    def test_missing_schema_or_space_id_does_not_continue(self):
        payload = self._unfinished()
        payload.pop("schema_valid")
        assert completion_can_continue(payload) is False
        payload["schema_valid"] = True
        payload["ego_task_space_id"] = None
        assert completion_can_continue(payload) is False

    def test_invalid_progress_types_do_not_continue(self):
        for field, value in (
            ("remaining", "2"),
            ("remaining", True),
            ("target", 0),
            ("published", 1.5),
        ):
            payload = self._unfinished(**{field: value})
            assert completion_can_continue(payload) is False

    def test_native_boundary_with_closed_space_recreates_when_safe(self):
        payload = self._unfinished(
            stop_reason="max_iterations",
            ego_cleanup="closed",
        )

        assert completion_can_continue(payload, exit_reason="max_iterations") is True

    def test_boundary_before_ego_space_creation_can_continue(self):
        payload = self._unfinished(
            stop_reason="segment_iteration_boundary",
            ego_task_space_id=None,
            ego_cleanup="not_created",
            segment_iteration_boundary=True,
        )

        assert completion_can_continue(payload, exit_reason="completed") is True

    def test_ego_space_id_must_be_positive_integer(self):
        for space_id in (0, -1, True, "7"):
            payload = self._unfinished(ego_task_space_id=space_id)
            assert completion_can_continue(payload) is False

    def test_progress_fingerprint_ignores_boundary_labels(self):
        first = self._unfinished()
        second = self._unfinished(stop_reason="segment_iteration_boundary")
        second["segment_iteration_boundary"] = True
        assert continuation_progress_fingerprint(first) == continuation_progress_fingerprint(
            second
        )


class TestFailedSegmentRecovery:
    @staticmethod
    def _entry(*trace, exit_reason="server_error"):
        return {
            "status": "failed",
            "exit_reason": exit_reason,
            "tool_trace": list(trace),
        }

    @staticmethod
    def _tool(name, status="ok"):
        return {"tool": name, "status": status}

    @staticmethod
    def _progress():
        payload = _round_payload()
        payload.update(schema_valid=True, exit_reason="max_iterations")
        return payload

    def test_recovers_when_provider_fails_after_successful_advance(self):
        entry = self._entry(
            self._tool("skill_view"),
            self._tool("backlinkhub_advance_submission_round"),
        )

        assert failed_segment_can_continue(entry, self._progress()) is True

    def test_recovers_when_browser_work_was_durably_recorded(self):
        entry = self._entry(
            self._tool("terminal"),
            self._tool("backlinkhub_record_submission_result"),
            self._tool("backlinkhub_advance_submission_round"),
        )

        assert failed_segment_can_continue(entry, self._progress()) is True

    def test_rejects_unrecorded_browser_work(self):
        entry = self._entry(
            self._tool("backlinkhub_advance_submission_round"),
            self._tool("terminal"),
        )

        assert failed_segment_can_continue(entry, self._progress()) is False

    def test_rejects_failed_or_unanswered_record(self):
        failed_record = self._entry(
            self._tool("terminal"),
            self._tool("backlinkhub_record_submission_result", "error"),
        )
        unanswered_record = self._entry(
            self._tool("terminal"),
            self._tool("backlinkhub_record_submission_result", ""),
        )

        assert failed_segment_can_continue(failed_record, self._progress()) is False
        assert failed_segment_can_continue(unanswered_record, self._progress()) is False

    def test_rejects_nontransport_failure_and_untrusted_progress(self):
        entry = self._entry(exit_reason="interrupted")
        untrusted = self._progress()
        untrusted["schema_valid"] = False

        assert failed_segment_can_continue(entry, self._progress()) is False
        assert failed_segment_can_continue(
            self._entry(), untrusted
        ) is False


class TestEmptyEgoCleanup:
    def test_uses_official_cli_and_returns_confirmed_close(self):
        completed = MagicMock(
            returncode=0,
            stdout='{"closed":true,"reason":"closed"}\n',
            stderr="",
        )
        with (
            patch("tools.delegate_tool.shutil.which", return_value="/bin/ego-browser"),
            patch("tools.delegate_tool.subprocess.run", return_value=completed) as run,
        ):
            result = _cleanup_empty_agent_ego_space(6)

        assert result == {"closed": True, "reason": "closed"}
        command = run.call_args.args[0]
        script = run.call_args.kwargs["input"]
        assert command == ["/bin/ego-browser", "nodejs"]
        assert "space.ownership !== 'agent'" in script
        assert "tabs.every" in script
        assert "'about:blank'" in script
        assert "completeTaskSpace(id, {keep: false})" in script

    def test_invalid_id_never_starts_ego(self):
        with patch("tools.delegate_tool.subprocess.run") as run:
            result = _cleanup_empty_agent_ego_space(True)

        assert result["reason"] == "invalid_space_id"
        run.assert_not_called()


# ---------------------------------------------------------------------------
# Tool-schema surface (one-time static field)
# ---------------------------------------------------------------------------


class TestToolSchemaSurface:
    def test_output_schema_on_task_items(self):
        item_props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"][
            "items"
        ]["properties"]
        assert "output_schema" in item_props
        assert item_props["output_schema"]["type"] == "object"
        # never required
        assert "output_schema" not in DELEGATE_TASK_SCHEMA["parameters"][
            "properties"
        ]["tasks"]["items"]["required"]

    def test_output_schema_on_top_level_goal_form(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        assert "output_schema" in props
        assert props["output_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# _run_single_child validation + bounded retry
# ---------------------------------------------------------------------------


class _StubChild:
    """Minimal child agent double (mirrors test_delegate_kanban_isolation)."""

    tool_progress_callback = None
    _delegate_saved_tool_names: list = []
    _credential_pool = None
    _subagent_id = None  # skip registry
    _delegate_depth = 1
    _parent_subagent_id = None
    _delegate_output_schema: dict | None = None
    model = "test-model"
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_estimated_cost_usd = 0.0
    session_reasoning_tokens = 0

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list = []

    def get_activity_summary(self):
        return {"api_call_count": 1, "max_iterations": 5, "current_tool": None}

    def run_conversation(self, user_message, task_id=None, **_kwargs):
        self.calls.append(user_message)
        response = self.responses.pop(0)
        if isinstance(response, dict):
            return response
        text = response
        return {
            "final_response": text,
            "completed": True,
            "api_calls": 1,
            "messages": [],
        }

    def close(self):
        return None


class _StubParent:
    _current_task_id = None
    _delegate_depth = 0

    def _touch_activity(self, _desc):
        return None


def _run(child):
    return _run_single_child(0, "produce the address", child, _StubParent())


class TestRunSingleChildSchemaValidation:
    def test_valid_first_try_no_retry(self):
        child = _StubChild(['{"city": "Berlin"}'])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["status"] == "completed"
        assert entry["schema_valid"] is True
        assert "schema_errors" not in entry
        assert len(child.calls) == 1

    def test_invalid_then_retry_then_valid(self):
        child = _StubChild(["not json at all", '{"city": "Oslo"}'])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["schema_valid"] is True
        assert entry["schema_retries"] == 1
        # retry turn carried the validation errors
        assert len(child.calls) == 2
        assert "rejected" in child.calls[1] or "JSON" in child.calls[1]
        # final summary is the retried (valid) answer
        assert json.loads(entry["summary"])["city"] == "Oslo"

    def test_valid_result_captures_continuation_metadata_before_finalization(self):
        schema = {
            "type": "object",
            "required": ["site_id", "run_id", "remaining"],
            "properties": {
                "site_id": {"type": "string"},
                "run_id": {"type": "string"},
                "remaining": {"type": "integer"},
                "ego_task_space_id": {"type": "integer"},
                "candidate_external_side_effect": {"type": "string"},
            },
        }
        child = _StubChild(
            [
                json.dumps(
                    {
                        "site_id": "site_thesitemath",
                        "run_id": "round_1",
                        "remaining": 2,
                        "ego_task_space_id": 7,
                        "candidate_external_side_effect": "none",
                    }
                )
            ]
        )
        child._delegate_output_schema = schema

        entry = _run(child)

        metadata = entry["_completion_metadata"]
        assert metadata["schema_valid"] is True
        assert metadata["run_id"] == "round_1"
        assert metadata["remaining"] == 2
        assert metadata["ego_task_space_id"] == 7
        assert metadata["candidate_external_side_effect"] == "none"

    def test_early_completed_backlink_result_is_marked_native_boundary(self):
        schema = {
            "type": "object",
            "required": [
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
                "ego_task_space_id",
                "ego_cleanup",
                "segment_iteration_boundary",
                "candidate_bound",
                "candidate_external_side_effect",
            ],
        }
        payload = TestContinuationBoundary._unfinished(
            stop_reason=(
                "stopped_without_native_termination_after_current_execution_segment"
            ),
            ego_cleanup="closed",
        )
        child = _StubChild([json.dumps(payload)])
        child._delegate_output_schema = schema

        entry = _run(child)

        assert entry["status"] == "completed"
        assert entry["exit_reason"] == "max_iterations"
        assert entry["truncated"] is True
        assert entry["_completion_metadata"]["exit_reason"] == "max_iterations"

    def test_legacy_progress_result_is_normalized_for_hot_reload(self):
        """An old session schema still produces resumable completion metadata."""
        schema = {
            "type": "object",
            "required": [
                "site_id",
                "run_id",
                "target",
                "progress",
                "queue_exhausted",
                "target_reached",
                "stop_reason",
                "segment_boundary",
                "candidate_bound",
            ],
            "properties": {
                "site_id": {"type": "string"},
                "run_id": {"type": "string"},
                "target": {"type": "integer"},
                "progress": {"type": "object"},
                "queue_exhausted": {"type": "boolean"},
                "target_reached": {"type": "boolean"},
                "stop_reason": {"type": "string"},
                "segment_boundary": {"type": "boolean"},
                "candidate_bound": {"type": "boolean"},
            },
        }
        child = _StubChild(
            [
                json.dumps(
                    {
                        "site_id": "site_thesitemath",
                        "run_id": "round_legacy",
                        "target": 6,
                        "progress": {
                            "published": 0,
                            "pending": 0,
                            "attempted_unconfirmed": 0,
                            "failed_retryable": 13,
                            "failed_final": 2,
                            "remaining": 6,
                        },
                        "queue_exhausted": False,
                        "target_reached": False,
                        "stop_reason": "segment_iteration_boundary",
                        "segment_boundary": True,
                        "candidate_bound": False,
                    }
                )
            ]
        )
        child._delegate_output_schema = schema

        metadata = _run(child)["_completion_metadata"]

        assert metadata["remaining"] == 6
        assert metadata["failed_retryable"] == 13
        assert metadata["segment_iteration_boundary"] is True
        assert metadata["stop_reason"] == "worker_returned_early"
        assert metadata["reported_stop_reason"] == "segment_iteration_boundary"
        assert metadata["candidate_external_side_effect"] == "none"

    def test_invalid_twice_surfaces_errors_and_stops(self):
        child = _StubChild(["nope", "still nope"])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["schema_valid"] is False
        assert entry["schema_errors"]
        assert entry["schema_retries"] == 1
        # exactly ONE retry — bounded
        assert len(child.calls) == 2

    def test_retry_exception_degrades_to_invalid(self):
        child = _StubChild(["nope"])
        child._delegate_output_schema = ADDRESS_SCHEMA

        original = child.run_conversation

        def flaky(user_message, task_id=None, **kw):
            if child.calls:
                raise RuntimeError("child died on retry")
            return original(user_message, task_id=task_id, **kw)

        child.run_conversation = flaky
        entry = _run(child)
        assert entry["schema_valid"] is False
        assert entry["schema_errors"]

    def test_no_schema_keeps_legacy_result_shape(self):
        """Schema-less calls must not gain new keys (wire-shape pinning)."""
        child = _StubChild(['{"city": "Berlin"}'])
        entry = _run(child)
        assert "schema_valid" not in entry
        assert "schema_errors" not in entry
        assert "schema_retries" not in entry
        assert len(child.calls) == 1

    def test_failed_child_skips_validation(self):
        """A child with no output never gets a schema retry turn."""
        child = _StubChild([""])
        child._delegate_output_schema = ADDRESS_SCHEMA
        entry = _run(child)
        assert entry["status"] == "failed"
        assert len(child.calls) == 1
        assert entry.get("schema_valid") is False

    def test_explicit_api_failure_with_text_is_not_completed(self):
        """A provider error must stay failed even when it has display text."""
        child = _StubChild(
            [
                {
                    "final_response": "API call failed after 3 retries: 502 Bad Gateway",
                    "completed": False,
                    "failed": True,
                    "error": "502 Bad Gateway",
                    "failure_reason": "server_error",
                    "api_calls": 3,
                    "messages": [],
                }
            ]
        )
        child._delegate_output_schema = ADDRESS_SCHEMA

        entry = _run(child)

        assert entry["status"] == "failed"
        assert entry["exit_reason"] == "server_error"
        assert entry["failure_reason"] == "server_error"
        assert "schema_retries" not in entry
        assert len(child.calls) == 1

    def test_failed_schema_result_does_not_trigger_retry(self):
        """Failed API output must not consume a schema-retry request."""
        child = _StubChild(
            [
                {
                    "final_response": "upstream unavailable",
                    "completed": False,
                    "failed": True,
                    "error": "upstream unavailable",
                    "api_calls": 1,
                    "messages": [],
                }
            ]
        )
        child._delegate_output_schema = ADDRESS_SCHEMA

        entry = _run(child)

        assert entry["status"] == "failed"
        assert "schema_retries" not in entry
        assert len(child.calls) == 1


# ---------------------------------------------------------------------------
# delegate_task dispatch-time schema handling
# ---------------------------------------------------------------------------


def _make_mock_parent():
    parent = MagicMock()
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    return parent


class TestDelegateTaskDispatch:
    def test_non_dict_output_schema_rejected(self):
        with (
            patch("tools.delegate_tool._load_config", return_value={}),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                return_value={
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "api_key": None,
                    "api_mode": None,
                },
            ),
        ):
            out = delegate_task(
                tasks=[
                    {"goal": "Summarize the release notes for module A", "output_schema": "not-a-dict"},
                    {"goal": "Summarize the release notes for module B"},
                ],
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert payload.get("error")
        assert "output_schema" in payload["error"]

    def test_invalid_json_schema_rejected_at_dispatch(self):
        with (
            patch("tools.delegate_tool._load_config", return_value={}),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                return_value={
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "api_key": None,
                    "api_mode": None,
                },
            ),
        ):
            out = delegate_task(
                tasks=[
                    {"goal": "Summarize the release notes for module A", "output_schema": {"type": 42}},
                    {"goal": "Summarize the release notes for module B"},
                ],
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert payload.get("error")
        assert "output_schema" in payload["error"]

    def test_child_receives_contract_and_schema_attr(self):
        """The built child carries the schema attr and its context gains
        the output-contract block."""
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            child = _StubChild(['{"city": "Rio"}'])
            return child

        with (
            patch("tools.delegate_tool._load_config", return_value={}),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                return_value={
                    "provider": None,
                    "model": None,
                    "base_url": None,
                    "api_key": None,
                    "api_mode": None,
                },
            ),
            patch(
                "tools.delegate_tool._build_child_preserving_parent_tools",
                side_effect=fake_build,
            ),
        ):
            out = delegate_task(
                goal="produce the address",
                context="base context",
                output_schema=ADDRESS_SCHEMA,
                parent_agent=_make_mock_parent(),
            )
        payload = json.loads(out)
        assert "OUTPUT CONTRACT" in (captured.get("context") or "")
        results = payload.get("results") or []
        assert results and results[0].get("schema_valid") is True


def _run_auto_continuation_scenario(payloads):
    children = []
    for index, payload in enumerate(payloads, start=1):
        if isinstance(payload, dict) and "_raw_child_response" in payload:
            response = payload["_raw_child_response"]
        else:
            response = {
                "final_response": json.dumps(payload),
                "completed": True,
                "api_calls": index,
                "messages": [],
            }
        child = _StubChild(
            [response]
        )
        child.model = "gpt-5.6-luna"
        child.session_prompt_tokens = index * 100
        child.session_completion_tokens = index * 10
        child.session_reasoning_tokens = index
        child.session_estimated_cost_usd = index / 1000
        child.session_cost_status = "estimated"
        child._delegate_role = "leaf"
        children.append(child)

    built_goals = []
    dispatched = []
    captured = {}

    def fake_build(**kwargs):
        built_goals.append(kwargs["goal"])
        return children[len(built_goals) - 1]

    def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        captured["combined"] = kwargs["runner"]()
        return {"status": "dispatched", "delegation_id": "deleg_auto_test"}

    parent = _make_mock_parent()
    parent.session_id = "parent-test"
    parent._current_task_id = "parent-task"
    parent._current_turn_id = "turn-test"
    parent._memory_manager = None
    parent._interrupt_requested = False
    parent.session_estimated_cost_usd = 0.0
    parent.session_cost_source = "none"
    parent.session_cost_status = "unknown"

    credentials = {
        "provider": None,
        "model": "gpt-5.6-luna",
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "request_overrides": None,
        "max_output_tokens": None,
        "command": None,
        "args": None,
    }
    config = {
        "max_iterations": 5,
        "tool_profiles": {"backlinkhub": ["terminal"]},
    }

    with (
        patch("tools.delegate_tool._load_config", return_value=config),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=credentials,
        ),
        patch(
            "tools.delegate_tool._build_child_preserving_parent_tools",
            side_effect=fake_build,
        ),
        patch(
            "tools.delegation_live_log.create_live_transcripts",
            return_value=(None, [], []),
        ),
        patch(
            "tools.async_delegation.dispatch_async_delegation_batch",
            side_effect=fake_dispatch,
        ),
        patch("gateway.session_context.async_delivery_supported", return_value=True),
        patch("gateway.session_context.get_session_env", return_value=""),
        patch("tools.approval.get_current_session_key", return_value="owner-test"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
        patch(
            "tools.delegate_tool._cleanup_empty_agent_ego_space",
            return_value={"closed": True, "reason": "closed"},
        ),
    ):
        handle = json.loads(
            delegate_task(
                goal="submit six backlinks for TheSiteMath",
                tool_profile="backlinkhub",
                background=True,
                output_schema=ROUND_SCHEMA,
                _auto_continue=True,
                parent_agent=parent,
            )
        )

    return handle, captured["combined"], dispatched, built_goals


class TestBacklinkAutoContinuation:
    @staticmethod
    def _transport_failure(*tools):
        messages = []
        for index, tool in enumerate(tools):
            call_id = f"call_{index}"
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "function": {"name": tool, "arguments": "{}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": '{"success":true}',
                    },
                ]
            )
        return {
            "_raw_child_response": {
                "final_response": "HTTP 500 after retries",
                "completed": False,
                "failed": True,
                "failure_reason": "server_error",
                "api_calls": 3,
                "messages": messages,
            }
        }

    def test_provider_failure_after_advance_recovers_same_round_once(self):
        terminal = _round_payload(
            published=1,
            pending=5,
            remaining=0,
            target_reached=True,
            stop_reason="target_reached",
            ego_cleanup="closed",
            segment_iteration_boundary=False,
        )

        _handle, combined, dispatched, built_goals = _run_auto_continuation_scenario(
            [
                _round_payload(),
                self._transport_failure("backlinkhub_advance_submission_round"),
                terminal,
            ]
        )

        assert len(dispatched) == 1
        assert len(built_goals) == 3
        assert "run_id=round_test" in built_goals[2]
        assert combined["results"][0]["continuation_segments"] == 3
        assert combined["continuation_history"][1]["transport_recovery"] == "scheduled"

    def test_same_progress_gets_only_one_transport_recovery(self):
        failure = self._transport_failure("backlinkhub_advance_submission_round")

        _handle, combined, _dispatched, built_goals = _run_auto_continuation_scenario(
            [_round_payload(), failure, failure]
        )

        assert len(built_goals) == 3
        assert combined["continuation_stop_reason"] == "transport_recovery_exhausted"
        assert combined["ego_recovery_cleanup"] == {
            "closed": True,
            "reason": "closed",
        }
        assert combined["continuation_last_progress"]["ego_cleanup"] == "closed"

    def test_safe_completed_candidate_handoff_continues_same_round(self):
        candidate_handoff = _round_payload(
            stop_reason="candidate_available",
            segment_iteration_boundary=False,
            candidate_bound=True,
            candidate_external_side_effect="none",
        )
        terminal = _round_payload(
            published=1,
            pending=5,
            remaining=0,
            target_reached=True,
            stop_reason="target_reached",
            ego_cleanup="closed",
            segment_iteration_boundary=False,
            candidate_bound=False,
            candidate_external_side_effect="none",
        )

        _handle, combined, dispatched, built_goals = _run_auto_continuation_scenario(
            [candidate_handoff, terminal]
        )

        assert len(dispatched) == 1
        assert len(built_goals) == 2
        assert "run_id=round_test" in built_goals[1]
        assert combined["results"][0]["continuation_segments"] == 2

    def test_safe_boundary_continues_inside_one_async_delegation(self):
        terminal = _round_payload(
            published=1,
            pending=5,
            remaining=0,
            target_reached=True,
            stop_reason="target_reached",
            ego_cleanup="closed",
            segment_iteration_boundary=False,
        )

        handle, combined, dispatched, built_goals = _run_auto_continuation_scenario(
            [_round_payload(), terminal]
        )

        assert handle["status"] == "dispatched"
        assert handle["delegation_id"] == "deleg_auto_test"
        assert len(dispatched) == 1
        assert len(built_goals) == 2
        assert "site_id=site_thesitemath" in built_goals[1]
        assert "run_id=round_test" in built_goals[1]
        assert "ego_task_space_id=7" in built_goals[1]
        assert 'skill_view(name="backlink-round-execution"' in built_goals[1]
        assert 'skill_view(name="ego-browser")' in built_goals[1]
        assert "不得重复加载" in built_goals[1]
        assert "第一项业务调用必须是" in built_goals[1]
        assert "省略 target_count" in built_goals[1]
        assert "run_id、site_id 和 target_count" not in built_goals[1]
        entry = combined["results"][0]
        assert entry["continuation_segments"] == 2
        assert entry["cumulative_api_calls"] == 3
        assert entry["cumulative_tokens"] == {"input": 300, "output": 30}
        assert [item["remaining"] for item in combined["continuation_history"]] == [
            5,
            0,
        ]

    def test_true_user_control_does_not_create_a_continuation_child(self):
        user_controlled = _round_payload(
            stop_reason="task_space_user_controlled",
            ego_cleanup="control_confirmation_required",
            segment_iteration_boundary=False,
        )

        _handle, combined, dispatched, built_goals = _run_auto_continuation_scenario(
            [user_controlled]
        )

        assert len(dispatched) == 1
        assert len(built_goals) == 1
        assert combined["results"][0]["continuation_segments"] == 1

    def test_closed_boundary_recreates_space_and_continues_same_round(self):
        observed_boundary = _round_payload(
            site_id="site_thesitemath",
            run_id="round_20260903_142813_c7c79100",
            target=6,
            published=0,
            pending=0,
            failed_retryable=8,
            failed_final=1,
            remaining=6,
            stop_reason="segment_iteration_boundary",
            ego_task_space_id=8,
            ego_cleanup="closed",
            segment_iteration_boundary=True,
            candidate_bound=False,
            candidate_external_side_effect="confirmed",
        )
        terminal = _round_payload(
            site_id="site_thesitemath",
            run_id="round_20260903_142813_c7c79100",
            target=6,
            published=0,
            pending=6,
            remaining=0,
            target_reached=True,
            stop_reason="target_reached",
            ego_task_space_id=9,
            ego_cleanup="closed",
            segment_iteration_boundary=False,
        )

        _handle, combined, dispatched, built_goals = _run_auto_continuation_scenario(
            [observed_boundary, terminal]
        )

        assert len(dispatched) == 1
        assert len(built_goals) == 2
        assert "ego_task_space_id=8" in built_goals[1]
        assert "创建同一轮次的替代空间" in built_goals[1]
        assert combined["results"][0]["continuation_segments"] == 2
        assert "continuation_error" not in combined

    def test_preserved_space_cannot_change_id_between_segments(self):
        changed_space = _round_payload(
            failed_retryable=3,
            ego_task_space_id=8,
        )
        terminal = _round_payload(
            pending=3,
            remaining=0,
            target_reached=True,
            stop_reason="target_reached",
            ego_task_space_id=8,
            ego_cleanup="closed",
            segment_iteration_boundary=False,
        )

        _handle, combined, _dispatched, built_goals = _run_auto_continuation_scenario(
            [_round_payload(), changed_space, terminal]
        )

        assert len(built_goals) == 2
        assert "changed a preserved Ego task space" in combined["continuation_error"]

    def test_repeated_progress_stops_after_one_recovery_segment(self):
        unchanged = _round_payload()

        _handle, combined, dispatched, built_goals = _run_auto_continuation_scenario(
            [unchanged, dict(unchanged)]
        )

        assert len(dispatched) == 1
        assert len(built_goals) == 2
        assert combined["continuation_stop_reason"] == "no_progress"
        assert combined["results"][0]["continuation_segments"] == 2
