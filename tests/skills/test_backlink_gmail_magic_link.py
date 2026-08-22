from __future__ import annotations

import argparse
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[2]
    / "skills"
    / "productivity"
    / "backlink-round-execution"
    / "scripts"
    / "gmail_magic_link.py"
)
SPEC = importlib.util.spec_from_file_location("backlink_gmail_magic_link", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
magic_link = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(magic_link)


def _message_with_html(value: str) -> dict:
    encoded = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    return {"payload": {"body": {"data": encoded}}}


def test_url_filter_accepts_only_exact_host_or_subdomain():
    message = _message_with_html(
        "https://login.example.com/verify?token=ok "
        "https://example.com.evil.test/verify?token=bad "
        "https://evil.test/verify?token=bad"
    )

    assert magic_link._urls_from_message(message, ("example.com",)) == [
        "https://login.example.com/verify?token=ok"
    ]


def test_open_in_browser_uses_only_fixed_binary(monkeypatch):
    calls = []
    monkeypatch.setattr(magic_link.Path, "is_file", lambda self: True)
    monkeypatch.setattr(
        magic_link.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(returncode=0, stdout="magic_link_opened"),
    )

    magic_link._open_in_browser("bh_site_run", "https://example.com/verify")

    assert calls[0][0] == [magic_link.EGO_BROWSER_BIN, "nodejs"]
    assert "https://example.com/verify" in calls[0][1]["input"]


def test_missing_fixed_binary_does_not_fall_back_to_path(monkeypatch):
    monkeypatch.setattr(magic_link.Path, "is_file", lambda self: False)
    monkeypatch.setattr(
        magic_link.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(magic_link.MagicLinkError) as exc_info:
        magic_link._open_in_browser("bh_site_run", "https://example.com/verify")

    assert exc_info.value.code == "ego_browser_unavailable"


def test_main_rejects_unsafe_task_space_before_gmail_access(monkeypatch, capsys):
    monkeypatch.setattr(
        magic_link,
        "_arguments",
        lambda: argparse.Namespace(
            session="bad task space; injected",
            after_epoch=int(magic_link.time.time()),
            link_host=["example.com"],
            sender_domain=[],
            subject_contains="",
            wait_seconds=0,
            poll_seconds=1,
            max_messages=1,
        ),
    )
    monkeypatch.setattr(
        magic_link,
        "_http_session",
        lambda: pytest.fail("Gmail must not be accessed"),
    )

    assert magic_link.main() == 2
    assert '"code": "invalid_ego_task_space"' in capsys.readouterr().out
