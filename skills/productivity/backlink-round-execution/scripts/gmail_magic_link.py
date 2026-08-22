#!/usr/bin/env python3
"""Open a verified Gmail magic link in an ego-browser task space.

The Gmail API is used only to locate a domain-validated link.  The message
body and URL never appear in stdout, evidence, or BacklinkHub records.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
EGO_BROWSER_BIN = str(Path.home() / ".local" / "bin" / "ego-browser")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
LINK_HINTS = (
    "magic",
    "verify",
    "verification",
    "confirm",
    "login",
    "log-in",
    "sign-in",
    "signin",
    "auth",
    "token",
    "session",
)


class MagicLinkError(RuntimeError):
    """A safe, user-actionable magic-link failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _emit(*, success: bool, code: str, **details: object) -> None:
    payload = {"success": success, "code": code, **details}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _normalize_host(value: str) -> str:
    candidate = str(value or "").strip().lower().rstrip(".")
    if not candidate:
        raise MagicLinkError("invalid_allowed_host")
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or any(char.isspace() for char in host):
        raise MagicLinkError("invalid_allowed_host")
    return host


def _matches_host(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}")
        for allowed in allowed_hosts
    )


def _system_proxy() -> str | None:
    override = os.getenv("HERMES_GMAIL_PROXY") or os.getenv("GMAIL_HTTP_PROXY")
    if override:
        return override
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if " : " not in line:
            continue
        key, value = line.strip().split(" : ", 1)
        values[key] = value.strip()
    if values.get("HTTPEnable") != "1":
        return None
    host = values.get("HTTPProxy", "")
    port = values.get("HTTPPort", "")
    if not host or not port.isdigit():
        return None
    return f"http://{host}:{port}"


def _http_session() -> Any:
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - environment setup failure
        raise MagicLinkError("gmail_dependencies_unavailable") from exc
    session = requests.Session()
    proxy = _system_proxy()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _token_path() -> Path:
    home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return home / "google_token.json"


def _load_access_token(session: Any) -> str:
    token_path = _token_path()
    if not token_path.is_file():
        raise MagicLinkError("gmail_not_authenticated")
    try:
        payload = json.loads(token_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MagicLinkError("gmail_token_invalid") from exc
    granted = set(payload.get("scopes") or [])
    if GMAIL_SCOPE not in granted:
        raise MagicLinkError("gmail_readonly_scope_missing")

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - environment setup failure
        raise MagicLinkError("gmail_dependencies_unavailable") from exc

    credentials = Credentials.from_authorized_user_file(
        str(token_path), [GMAIL_SCOPE]
    )
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request(session=session))
        except Exception as exc:
            raise MagicLinkError("gmail_token_refresh_failed") from exc
        refreshed = json.loads(credentials.to_json())
        refreshed["type"] = "authorized_user"
        refreshed["scopes"] = list(payload.get("scopes") or [GMAIL_SCOPE])
        refreshed["requested_scopes"] = list(
            payload.get("requested_scopes") or refreshed["scopes"]
        )
        temporary = token_path.with_suffix(".tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(refreshed, handle, indent=2)
        os.chmod(temporary, 0o600)
        temporary.replace(token_path)
    if not credentials.valid:
        raise MagicLinkError("gmail_token_invalid")
    return str(credentials.token)


def _gmail_get(session: Any, token: str, url: str, params: object) -> dict[str, Any]:
    try:
        response = session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=20,
        )
    except Exception as exc:
        raise MagicLinkError("gmail_api_unavailable") from exc
    if response.status_code == 403:
        raise MagicLinkError("gmail_api_denied")
    if response.status_code != 200:
        raise MagicLinkError("gmail_api_unavailable")
    try:
        value = response.json()
    except ValueError as exc:
        raise MagicLinkError("gmail_api_unavailable") from exc
    if not isinstance(value, dict):
        raise MagicLinkError("gmail_api_unavailable")
    return value


def _headers(message: dict[str, Any]) -> dict[str, str]:
    raw_headers = message.get("payload", {}).get("headers", [])
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in raw_headers
        if isinstance(header, dict)
    }


def _message_text(part: dict[str, Any]) -> str:
    content: list[str] = []
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    encoded = body.get("data")
    if isinstance(encoded, str) and encoded:
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            content.append(
                base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
            )
        except (ValueError, UnicodeDecodeError):
            pass
    for child in part.get("parts", []) if isinstance(part.get("parts"), list) else []:
        if isinstance(child, dict):
            content.append(_message_text(child))
    return "\n".join(piece for piece in content if piece)


def _urls_from_message(message: dict[str, Any], allowed_hosts: tuple[str, ...]) -> list[str]:
    text = html.unescape(_message_text(message.get("payload", {})))
    found: list[str] = []
    for raw_url in URL_RE.findall(text):
        url = raw_url.rstrip(".,;:!?)]}>'\"")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if not _matches_host(parsed.hostname, allowed_hosts):
            continue
        found.append(url)
    return found


def _score_url(url: str, allowed_hosts: tuple[str, ...]) -> int:
    parsed = urlsplit(url)
    value = f"{parsed.path}?{parsed.query}".lower()
    score = sum(10 for hint in LINK_HINTS if hint in value)
    if parsed.hostname and parsed.hostname.lower() in allowed_hosts:
        score += 3
    if "unsubscribe" in value:
        score -= 100
    return score


def _sender_matches(value: str, sender_domains: tuple[str, ...]) -> bool:
    if not sender_domains:
        return True
    _, address = parseaddr(value)
    if "@" not in address:
        return False
    return _matches_host(address.rsplit("@", 1)[1], sender_domains)


def _received_epoch(metadata: dict[str, Any]) -> int | None:
    """Return Gmail's received timestamp, skipping malformed metadata safely."""

    try:
        milliseconds = int(str(metadata.get("internalDate", "")))
    except (TypeError, ValueError):
        return None
    return max(0, milliseconds // 1000)


def _find_magic_link(
    session: Any,
    token: str,
    *,
    after_epoch: int,
    allowed_hosts: tuple[str, ...],
    sender_domains: tuple[str, ...],
    subject_contains: str,
    max_messages: int,
) -> tuple[str, str, int] | None:
    listing = _gmail_get(
        session,
        token,
        GMAIL_MESSAGES_URL,
        {"q": "newer_than:2d", "maxResults": max_messages},
    )
    best: tuple[int, int, str, str] | None = None
    for item in listing.get("messages", []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        message_id = str(item["id"])
        metadata = _gmail_get(
            session,
            token,
            f"{GMAIL_MESSAGES_URL}/{message_id}",
            [
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Subject"),
            ],
        )
        received_epoch = _received_epoch(metadata)
        if received_epoch is None:
            continue
        if received_epoch < after_epoch:
            continue
        headers = _headers(metadata)
        if not _sender_matches(headers.get("from", ""), sender_domains):
            continue
        subject = headers.get("subject", "").lower()
        if subject_contains and subject_contains.lower() not in subject:
            continue
        full_message = _gmail_get(
            session,
            token,
            f"{GMAIL_MESSAGES_URL}/{message_id}",
            {"format": "full"},
        )
        for url in _urls_from_message(full_message, allowed_hosts):
            score = _score_url(url, allowed_hosts)
            candidate = (score, received_epoch, message_id, url)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None or best[0] < 0:
        return None
    return best[2], best[3], best[1]


def _open_in_browser(session_name: str, url: str) -> None:
    """Open the validated URL in the caller's ego task space."""
    ego_browser = EGO_BROWSER_BIN
    if not Path(ego_browser).is_file():
        raise MagicLinkError("ego_browser_unavailable")
    script = (
        "const task = await useOrCreateTaskSpace(%s);\n"
        "await openOrReuseTab(%s, {wait: true, timeout: 20});\n"
        "cliLog('magic_link_opened');\n"
    ) % (json.dumps(session_name), json.dumps(url))
    try:
        completed = subprocess.run(
            [ego_browser, "nodejs"],
            input=script,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MagicLinkError("ego_magic_link_open_failed") from exc
    if completed.returncode != 0 or "magic_link_opened" not in completed.stdout:
        raise MagicLinkError("ego_magic_link_open_failed")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open a domain-validated Gmail magic link in an ego task space."
    )
    parser.add_argument("--session", required=True)
    parser.add_argument("--after-epoch", type=int, required=True)
    parser.add_argument("--link-host", action="append", required=True)
    parser.add_argument("--sender-domain", action="append", default=[])
    parser.add_argument("--subject-contains", default="")
    parser.add_argument("--wait-seconds", type=int, default=45)
    parser.add_argument("--poll-seconds", type=int, default=3)
    parser.add_argument("--max-messages", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    now = int(time.time())
    if not SESSION_NAME_RE.fullmatch(args.session.strip()):
        _emit(success=False, code="invalid_ego_task_space")
        return 2
    if args.after_epoch > now + 60 or args.after_epoch < now - 172800:
        _emit(success=False, code="invalid_magic_link_time_window")
        return 2
    wait_seconds = max(0, min(args.wait_seconds, 60))
    poll_seconds = max(1, min(args.poll_seconds, 10))
    max_messages = max(1, min(args.max_messages, 100))
    try:
        allowed_hosts = tuple(dict.fromkeys(_normalize_host(value) for value in args.link_host))
        sender_domains = tuple(
            dict.fromkeys(_normalize_host(value) for value in args.sender_domain)
        )
        session = _http_session()
        token = _load_access_token(session)
        deadline = time.monotonic() + wait_seconds
        while True:
            candidate = _find_magic_link(
                session,
                token,
                after_epoch=args.after_epoch,
                allowed_hosts=allowed_hosts,
                sender_domains=sender_domains,
                subject_contains=args.subject_contains,
                max_messages=max_messages,
            )
            if candidate is not None:
                message_id, url, received_epoch = candidate
                _open_in_browser(args.session, url)
                _emit(
                    success=True,
                    code="magic_link_opened",
                    link_host=_normalize_host(urlsplit(url).hostname or ""),
                    message_ref=hashlib.sha256(message_id.encode()).hexdigest()[:16],
                    received_after_request=received_epoch >= args.after_epoch,
                )
                return 0
            if time.monotonic() >= deadline:
                _emit(success=False, code="magic_link_not_found")
                return 3
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
    except MagicLinkError as exc:
        _emit(success=False, code=exc.code)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
