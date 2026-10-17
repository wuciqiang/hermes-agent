"""Host-side identity binding for BacklinkHub delegation."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Dict, List, Optional


_RUN_ID_RE = re.compile(
    r"\brun_id\s*[:=]\s*[\"']?([^\s\"',;]*)",
    re.IGNORECASE,
)
_PLACEHOLDER_RUN_IDS = frozenset(
    {"", "?", "new", "unknown", "null", "none", "n/a", "na", "tbd"}
)


def _run_id_is_concrete(value: object) -> bool:
    run_id = str(value or "").strip()
    return bool(
        run_id
        and run_id.casefold() not in _PLACEHOLDER_RUN_IDS
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", run_id)
    )


def _new_run_id() -> str:
    """Allocate one stable identity at the host dispatch boundary."""

    return f"round_{time.strftime('%Y%m%d_%H%M%S', time.localtime())}_{uuid.uuid4().hex[:8]}"


def _replace_placeholder_run_id(text: str, run_id: str) -> str:
    """Replace only placeholder run_id values while preserving surrounding text."""

    def replace(match: re.Match[str]) -> str:
        raw_value = match.group(1)
        normalized = raw_value.rstrip(",.;:")
        if normalized.casefold() not in _PLACEHOLDER_RUN_IDS:
            return match.group(0)
        start, end = match.span(1)
        suffix = raw_value[len(normalized):]
        replacement = run_id + suffix
        return match.group(0)[: start - match.start()] + replacement + match.group(0)[end - match.start() :]

    return _RUN_ID_RE.sub(replace, text)


def prepare_dispatch_identity(
    task_list: List[Dict[str, Any]], shared_context: Optional[str]
) -> tuple[List[Dict[str, Any]], List[str], Optional[str]]:
    """Bind concrete run IDs to child context before a worker is built.

    BacklinkHub rejects ``new`` and other placeholders at its service boundary.
    The model receives the durable identity in the persisted task context and
    reuses it across every continuation.
    """

    run_ids: List[str] = []
    shared = str(shared_context or "")
    for task in task_list:
        task_context = str(task.get("context") or "")
        source = task_context or (shared if len(task_list) == 1 else "")
        candidate = ""
        for match in _RUN_ID_RE.finditer(source):
            value = match.group(1).rstrip(",.;:")
            if _run_id_is_concrete(value):
                candidate = value
                break
        if not candidate:
            for match in _RUN_ID_RE.finditer(str(task.get("goal") or "")):
                value = match.group(1).rstrip(",.;:")
                if _run_id_is_concrete(value):
                    candidate = value
                    break
        run_id = candidate or _new_run_id()
        run_ids.append(run_id)
        task["goal"] = _replace_placeholder_run_id(str(task.get("goal") or ""), run_id)
        task_context = _replace_placeholder_run_id(task_context, run_id)
        identity_note = (
            "Host allocated BacklinkHub identity for this delegation: "
            f"run_id={run_id}. Reuse this exact run_id for every advance and "
            "record call in this site task; never substitute new or another ID."
        )
        task["context"] = (
            f"{task_context.rstrip()}\n\n{identity_note}"
            if task_context.strip()
            else identity_note
        )

    persisted_context = shared_context
    if len(task_list) == 1 and run_ids:
        persisted_context = task_list[0]["context"]
    elif len(task_list) > 1 and run_ids:
        manifest = "Host allocated BacklinkHub run IDs by task index: " + ", ".join(
            f"{index}={run_id}" for index, run_id in enumerate(run_ids)
        )
        persisted_context = f"{shared.rstrip()}\n\n{manifest}" if shared.strip() else manifest
    return task_list, run_ids, persisted_context


__all__ = ["prepare_dispatch_identity"]
