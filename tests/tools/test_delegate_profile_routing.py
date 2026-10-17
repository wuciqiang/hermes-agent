from unittest.mock import MagicMock, patch

import tools.delegate_tool as delegate_tool
from tools.delegate_tool_backlinkhub import prepare_dispatch_identity
from tools.delegate_tool_routing import resolve_profile_route


def test_delegate_entry_selects_profile_route_and_preserves_global_route(monkeypatch):
    overrides = {
        "extra_body": {"thinking": {"type": "enabled", "clear_thinking": False}},
    }
    config = {
        "provider": "codeplan",
        "profile_routes": {
            "backlinkhub": {
                "provider": "backlink-glm",
                "model": "glm-5.3-flash",
                "reasoning_effort": "low",
                "fallback_providers": [],
                "request_overrides": overrides,
            }
        },
    }

    config["max_iterations"] = 3
    config["max_concurrent_children"] = 1
    config["tool_profiles"] = {"backlinkhub": ["terminal"]}
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: config)
    monkeypatch.setattr(delegate_tool, "_resolve_tool_profile", lambda name, cfg: ["terminal"])
    monkeypatch.setattr(delegate_tool, "_get_max_concurrent_children", lambda: 1)
    monkeypatch.setattr(delegate_tool, "_normalize_task_list", lambda *args, **kwargs: ([{"goal": "x"}], None))
    monkeypatch.setattr(delegate_tool, "_coerce_task_schemas", lambda tasks, schema: ([], None))
    monkeypatch.setattr(delegate_tool, "_announce_batch", lambda *args, **kwargs: None)
    monkeypatch.setattr(delegate_tool, "_capture_origin", lambda: ("", "", "", ""))
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 1)
    captured = {}
    monkeypatch.setattr(
        delegate_tool,
        "_build_children",
        lambda tasks, schemas, creds, **kwargs: (
            captured.update(creds=creds, tasks=tasks) or [],
            None,
        ),
    )
    monkeypatch.setattr(delegate_tool, "_run_batch", lambda batch, background: {"ok": True})

    parent = MagicMock()
    parent._delegate_depth = 0
    parent._fallback_chain = [{"provider": "codeplan", "model": "global-fallback"}]
    parent.request_overrides = None

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
        "provider": "custom", "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
        "api_key": "key", "api_mode": "chat_completions", "model": "glm-5.3-flash",
        "request_overrides": {},
    }) as resolve_runtime:
        result = delegate_tool.delegate_task(
            goal="x", background=False, tool_profile="backlinkhub", parent_agent=parent
        )
    assert result == {"ok": True}
    resolve_runtime.assert_called_once_with(requested="backlink-glm", target_model="glm-5.3-flash")
    assert captured["creds"]["provider"] == "backlink-glm"
    assert captured["creds"]["model"] == "glm-5.3-flash"
    assert captured["creds"]["request_overrides"]["extra_body"]["thinking"]["clear_thinking"] is False
    assert "Host allocated BacklinkHub identity" in captured["tasks"][0]["context"]
    assert "run_id=round_" in captured["tasks"][0]["context"]

    route = resolve_profile_route(config, "backlinkhub")
    route["request_overrides"]["extra_body"]["thinking"]["type"] = "mutated"
    assert overrides["extra_body"]["thinking"]["type"] == "enabled"

    from agent.chat_completion_helpers import build_api_kwargs
    from run_agent import AIAgent
    agent = AIAgent(
        model="glm-5.3-flash", provider="custom",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4", api_key="test-key",
        api_mode="chat_completions", request_overrides=overrides,
        enabled_toolsets=[], quiet_mode=True, skip_context_files=True, skip_memory=True,
    )
    try:
        kwargs = build_api_kwargs(agent, [], [])
        assert kwargs["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": False}
        assert "clear_thinking" not in kwargs
    finally:
        agent.close()


def test_explicit_credentials_route_wins_over_profile(monkeypatch):
    config = {
        "max_iterations": 3,
        "max_concurrent_children": 1,
        "tool_profiles": {"backlinkhub": ["terminal"]},
        "profile_routes": {"backlinkhub": {"provider": "backlink-glm", "model": "glm-5.3-flash"}},
    }
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: config)
    monkeypatch.setattr(delegate_tool, "_resolve_tool_profile", lambda name, cfg: ["terminal"])
    monkeypatch.setattr(delegate_tool, "_get_max_concurrent_children", lambda: 1)
    monkeypatch.setattr(delegate_tool, "_normalize_task_list", lambda *args, **kwargs: ([{"goal": "x"}], None))
    monkeypatch.setattr(delegate_tool, "_coerce_task_schemas", lambda tasks, schema: ([], None))
    monkeypatch.setattr(delegate_tool, "_announce_batch", lambda *args, **kwargs: None)
    monkeypatch.setattr(delegate_tool, "_capture_origin", lambda: ("", "", "", ""))
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 1)
    monkeypatch.setattr(delegate_tool, "_build_children", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(delegate_tool, "_run_batch", lambda batch, background: {"ok": True})
    parent = MagicMock(_delegate_depth=0, request_overrides=None, _fallback_chain=[])

    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value={
        "provider": "custom", "base_url": "https://example.test/v1", "api_key": "key",
        "api_mode": "chat_completions", "model": "explicit-model", "request_overrides": {},
    }) as resolve_runtime:
        result = delegate_tool.delegate_task(
            goal="x", background=False, tool_profile="backlinkhub", parent_agent=parent,
            credentials_cfg={"provider": "explicit-provider", "model": "explicit-model"},
        )
    assert result == {"ok": True}
    resolve_runtime.assert_called_once_with(requested="explicit-provider", target_model="explicit-model")


def test_backlinkhub_dispatch_allocates_and_persists_concrete_run_id():
    tasks, run_ids, persisted = prepare_dispatch_identity(
        [{"goal": "run one site run_id=?", "context": "run_id=new"}],
        "shared context",
    )

    assert len(run_ids) == 1
    assert run_ids[0].startswith("round_")
    assert run_ids[0] != "new"
    assert f"run_id={run_ids[0]}" in tasks[0]["goal"]
    assert "run_id=?" not in tasks[0]["goal"]
    assert "run_id=new" not in tasks[0]["goal"]
    assert "run_id=new" not in tasks[0]["context"]
    assert f"run_id={run_ids[0]}" in tasks[0]["context"]
    assert persisted == tasks[0]["context"]


def test_backlinkhub_dispatch_reuses_concrete_run_id():
    tasks, run_ids, persisted = prepare_dispatch_identity(
        [{"goal": "resume one site", "context": "continue run_id=round_20261016_120000_ab12cd34"}],
        None,
    )

    assert run_ids == ["round_20261016_120000_ab12cd34"]
    assert tasks[0]["context"].count(run_ids[0]) == 2
    assert persisted == tasks[0]["context"]
