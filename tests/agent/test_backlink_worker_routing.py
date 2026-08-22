from __future__ import annotations

import json
from types import SimpleNamespace

from agent.agent_runtime_helpers import route_backlink_submission_to_worker
from tools.delegate_tool import _release_backlink_workers, _reserve_backlink_workers


def _call(name: str, args: dict, call_id: str = "call-1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


class _Parent:
    platform = "feishu"
    _subagent_id = None
    session_id = "test-backlink-parent"

    def __init__(self):
        self.handoffs = []

    def _dispatch_delegate_task(self, args):
        self.handoffs.append(args)
        return json.dumps({"worker": "completed", "model": "gpt-5.6-luna"})


class _ReservingParent(_Parent):
    def _dispatch_delegate_task(self, args):
        self.handoffs.append(args)
        keys, error = _reserve_backlink_workers(
            self,
            [args],
            args.get("toolsets"),
        )
        try:
            if error:
                return json.dumps({"error": error})
            return json.dumps({"worker": "reserved", "model": "gpt-5.6-luna"})
        finally:
            _release_backlink_workers(keys)


def test_parent_advance_is_routed_to_one_station_worker():
    parent = _Parent()
    calls = [_call("backlinkhub_advance_submission_round", {"site_id": "site_math"})]
    messages = [{"role": "user", "content": "提交 https://thesitemath.com/，目标 3 条"}]

    routed = route_backlink_submission_to_worker(parent, calls, messages)

    assert routed is not None
    assert json.loads(routed["call-1"])["worker"] == "completed"
    assert len(parent.handoffs) == 1
    handoff = parent.handoffs[0]
    assert handoff["toolsets"] == ["terminal", "backlinkhub"]
    assert "thesitemath.com" in handoff["context"]
    assert "backlink_site_reference=site_math" in handoff["context"]
    assert "backlink_run_reference=*" in handoff["context"]
    assert "不要自行拼接 site_ 前缀" in handoff["context"]
    assert "ego-browser" in handoff["context"]
    assert "`bh_<site_id>_<run_id>` 一个 ego 任务空间" in handoff["context"]
    assert "completeTaskSpace" in handoff["context"]


def test_domain_reference_is_forwarded_without_guessing_a_site_id():
    parent = _Parent()
    calls = [_call(
        "backlinkhub_advance_submission_round",
        {"site_id": "spritepacker.app"},
    )]
    messages = [{"role": "user", "content": "提交 https://spritepacker.app/ 的外链"}]

    routed = route_backlink_submission_to_worker(parent, calls, messages)

    assert routed is not None
    assert len(parent.handoffs) == 1
    context = parent.handoffs[0]["context"]
    assert "backlink_site_reference=spritepacker.app" in context
    assert "site_spritepacker" not in context


def test_first_domain_advance_reaches_worker_reservation_without_run_id():
    parent = _ReservingParent()
    parent.session_id = "test-domain-first-advance"
    calls = [_call(
        "backlinkhub_advance_submission_round",
        {"site_id": "m3u8-tomp4.com"},
    )]
    messages = [{"role": "user", "content": "提交 m3u8-tomp4.com 的外链"}]

    routed = route_backlink_submission_to_worker(parent, calls, messages)

    assert routed is not None
    assert json.loads(routed["call-1"])["worker"] == "reserved"
    assert len(parent.handoffs) == 1


def test_station_worker_is_not_routed_again():
    parent = _Parent()
    parent.platform = "subagent"
    calls = [_call("backlinkhub_advance_submission_round", {})]

    assert route_backlink_submission_to_worker(parent, calls, []) is None
    assert parent.handoffs == []


def test_parent_cannot_write_back_directly():
    parent = _Parent()
    calls = [
        _call(
            "backlinkhub_record_submission_result",
            {
                "run_id": "round_1",
                "work_item_id": "wri_1",
                "site_id": "site_math",
                "platform_id": "p1",
                "outcome": "pending",
            },
        )
    ]

    routed = route_backlink_submission_to_worker(parent, calls, [])

    assert routed is not None
    payload = json.loads(routed["call-1"])
    assert payload["error"] == "backlinkhub_parent_write_forbidden"
    assert parent.handoffs == []


def test_same_user_turn_reuses_worker_summary_without_spawning_again():
    parent = _Parent()
    calls = [_call(
        "backlinkhub_advance_submission_round",
        {"site_id": "site_reuse", "run_id": "round_reuse_1"},
    )]
    messages = [{"role": "user", "content": "提交 site_reuse"}]

    first = route_backlink_submission_to_worker(parent, calls, messages)
    second = route_backlink_submission_to_worker(parent, calls, messages)

    assert first is not None and second is not None
    assert len(parent.handoffs) == 1
    assert json.loads(second["call-1"])["status"] == "worker_already_ran"


def test_first_advance_without_run_id_is_still_routed_only_once():
    parent = _Parent()
    calls = [_call(
        "backlinkhub_advance_submission_round",
        {"site_id": "site_first_advance"},
    )]
    messages = [{"role": "user", "content": "开始新一轮提交"}]

    route_backlink_submission_to_worker(parent, calls, messages)
    cached = route_backlink_submission_to_worker(parent, calls, messages)

    assert len(parent.handoffs) == 1
    assert json.loads(cached["call-1"])["status"] == "worker_already_ran"


def test_discovered_run_identity_stays_on_the_same_user_turn():
    parent = _Parent()
    messages = [{"role": "user", "content": "提交新站外链"}]
    first = [_call(
        "backlinkhub_advance_submission_round",
        {},
    )]
    second = [_call(
        "backlinkhub_advance_submission_round",
        {"site_id": "site_discovered", "run_id": "round_discovered_1"},
    )]

    route_backlink_submission_to_worker(parent, first, messages)
    cached = route_backlink_submission_to_worker(parent, second, messages)

    assert len(parent.handoffs) == 1
    assert json.loads(cached["call-1"])["status"] == "worker_already_ran"


def test_browser_control_stop_is_sticky_for_the_same_round():
    parent = _Parent()

    def stop_worker(_args):
        parent.handoffs.append(_args)
        return "EGO_TASK_SPACE_USER_IN_CONTROL: user owns task space"

    parent._dispatch_delegate_task = stop_worker
    calls = [_call(
        "backlinkhub_advance_submission_round",
        {"site_id": "site_stop", "run_id": "round_stop_1"},
    )]
    messages = [{"role": "user", "content": "提交 site_stop"}]

    route_backlink_submission_to_worker(parent, calls, messages)
    blocked = route_backlink_submission_to_worker(parent, calls, messages)

    assert len(parent.handoffs) == 1
    blocked_payload = json.loads(blocked["call-1"])
    assert blocked_payload["error"] == "backlinkhub_browser_control_blocked"
    assert "控制权发生切换" in blocked_payload["message"]
    assert "继续同一轮次" in blocked_payload["next_action"]
    assert "被用户接管" not in blocked_payload["message"]

    resumed_messages = [{"role": "user", "content": "任务空间已释放，继续同一轮"}]
    route_backlink_submission_to_worker(parent, calls, resumed_messages)
    assert len(parent.handoffs) == 2
