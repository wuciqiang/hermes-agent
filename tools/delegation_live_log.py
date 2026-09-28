"""Live, tail-able transcripts for delegated subagents.

One append-only log per child under ``<hermes_home>/cache/delegation/live/
<delegation_id>/task-<n>.log``, pre-created with a header at dispatch (so
``tail -f`` attaches immediately); paths are returned from ``delegate_task``.
``cache/delegation`` is mounted read-only into remote terminal backends, so
every line written here must be credential-redacted. Never raises into the
agent loop; append mode per write (close() is the flush); 7-day retention.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LIVE_RETENTION_DAYS = 7

# Manifest writes are intentionally tiny but can be triggered by both the
# child progress relay and the async recovery thread.  Serializing the
# read/modify/write cycle keeps a checkpoint from being lost when a tool
# completion and a terminal recovery notice arrive together.
_MANIFEST_LOCK = threading.RLock()

# Per-line truncation budgets (chars): the .log is a compact operational view;
# the child's SessionDB transcript and summary spill files carry full text.
_ASSISTANT_MAX = 600
_THINKING_MAX = 300
_ARGS_MAX = 220
_RESULT_MAX = 400
_KICKOFF_MAX = 500
# Stream deltas are buffered and flushed as one assistant line when another
# event type arrives (or on completion); capped so a huge reply can't hold memory.
_STREAM_BUFFER_FLUSH_CHARS = 4000
_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def live_transcript_root() -> Path:
    """Root directory for live transcripts (profile-safe, never ~/.hermes)."""
    from hermes_constants import get_hermes_dir
    return get_hermes_dir("cache/delegation", "delegation_cache") / "live"


@contextmanager
def _best_effort(what: str):
    """Swallow and debug-log any failure: nothing here may reach the agent loop."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        logger.debug("Live transcript %s failed: %s", what, exc)


def _one_line(text: Any, limit: int) -> str:
    """Collapse to a single line and truncate with an elided-chars note."""
    s = " ".join(str(text or "").split())
    if len(s) > limit:
        s = s[:limit] + f" …(+{len(s) - limit} chars)"
    return s


def _redact(text: str) -> str:
    """Mask credentials (``force=True``: safety boundary, even when the global
    toggle is off); if the redactor is unavailable, withhold rather than leak."""
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True) or ""
    except Exception:  # pragma: no cover - core module; never leak on failure
        return "[line withheld: redaction unavailable]"


def _joined(*parts: str) -> str:
    return " ".join(filter(None, parts))


def _dump_json(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically publish a manifest so a crash cannot leave partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


_CHECKPOINT_STAGES = frozenset({
    "segment_started",
    "advance_started", "advanced", "advance_failed", "browser_started",
    "stage_started", "final_action_started", "record_started", "recorded", "record_failed",
})


def _stage_from_runtime_metric(payload: Any) -> Optional[str]:
    """Read a bounded candidate checkpoint encoded in a runtime metric reason."""
    if not isinstance(payload, dict):
        return None
    reason = str(payload.get("reason") or "").strip().lower()
    prefix = "candidate_stage:"
    if not reason.startswith(prefix):
        return None
    stage = reason.removeprefix(prefix).strip()
    return stage if stage in _CHECKPOINT_STAGES else None


def _update_manifest_checkpoint(
    delegation_id: str, task_index: int, stage: str, details: str = ""
) -> None:
    """Best-effort atomic-ish manifest checkpoint update.

    The live transcript remains the full operational trace; this small field
    is the recovery index used after an owner process disappears.  It is kept
    deliberately independent of business outcomes so a checkpoint can never
    be mistaken for a published backlink.
    """
    if stage not in _CHECKPOINT_STAGES:
        return
    with _best_effort("manifest checkpoint"):
        with _MANIFEST_LOCK:
            path = _manifest_path(delegation_id)
            if not path.is_file():
                return
            manifest = json.loads(path.read_text(encoding="utf-8"))
            tasks = manifest.get("tasks")
            if not isinstance(tasks, list):
                return
            now = time.strftime(_TIME_FMT)
            for task in tasks:
                if task.get("index") == task_index:
                    task["checkpoint"] = {
                        "stage": stage,
                        "at": now,
                        **({"details": details} if details else {}),
                    }
                    break
            manifest["updated"] = now
            _dump_json(path, manifest)


def _update_manifest_tool_facts(
    delegation_id: str, task_index: int, facts: Dict[str, Dict[str, Any]]
) -> None:
    """Persist the current segment's structured BacklinkHub tool results."""
    with _best_effort("manifest tool facts"):
        with _MANIFEST_LOCK:
            path = _manifest_path(delegation_id)
            if not path.is_file():
                return
            manifest = json.loads(path.read_text(encoding="utf-8"))
            now = time.strftime(_TIME_FMT)
            for task in manifest.get("tasks", []):
                if task.get("index") == task_index:
                    task["tool_facts"] = facts
                    break
            manifest["updated"] = now
            _dump_json(path, manifest)


def recover_live_manifest(
    delegation_id: Optional[str], *, reason: str, results: Optional[List[Dict[str, Any]]] = None
) -> None:
    """Converge a manifest whose owner process died before normal finalization."""
    if not delegation_id:
        return
    with _best_effort("manifest recovery"):
        with _MANIFEST_LOCK:
            path = _manifest_path(str(delegation_id))
            if not path.is_file():
                return
            manifest = json.loads(path.read_text(encoding="utf-8"))
            by_index = {
                item.get("task_index"): item
                for item in (results or [])
                if isinstance(item, dict)
            }
            now = time.strftime(_TIME_FMT)
            for task in manifest.get("tasks", []):
                if task.get("status") != "running":
                    continue
                result = by_index.get(task.get("index"), {})
                task["status"] = result.get("status") or "abandoned"
                task["exit_reason"] = reason
                task["recovered_at"] = now
                task["recovery_required"] = True
            manifest["status"] = "recovered"
            manifest["recovery_reason"] = reason
            manifest["completed"] = now
            manifest["updated"] = now
            _dump_json(path, manifest)


def get_manifest_checkpoints(delegation_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return the last checkpoint for each live task, if a manifest exists."""
    if not delegation_id:
        return []
    with _best_effort("manifest checkpoint read"):
        with _MANIFEST_LOCK:
            path = _manifest_path(str(delegation_id))
            if not path.is_file():
                return []
            manifest = json.loads(path.read_text(encoding="utf-8"))
            checkpoints = []
            for task in manifest.get("tasks", []):
                checkpoint = task.get("checkpoint")
                if isinstance(checkpoint, dict):
                    checkpoints.append({
                        "task_index": task.get("index"),
                        **checkpoint,
                    })
            return checkpoints
    return []


def get_manifest_tool_facts(delegation_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return structured tool results captured during the current segment."""
    if not delegation_id:
        return []
    with _best_effort("manifest tool facts read"):
        with _MANIFEST_LOCK:
            path = _manifest_path(str(delegation_id))
            if not path.is_file():
                return []
            manifest = json.loads(path.read_text(encoding="utf-8"))
            facts = []
            for task in manifest.get("tasks", []):
                task_facts = task.get("tool_facts")
                if isinstance(task_facts, dict):
                    facts.append({"task_index": task.get("index"), **task_facts})
            return facts
    return []


class LiveTranscriptWriter:
    """Append-only event log for ONE subagent task. Best-effort: the first write
    failure flips ``_ok`` off and later calls become debug-logged no-ops."""

    def __init__(self, delegation_id: str, task_index: int, goal: str,
                 context: Optional[str] = None, root: Optional[Path] = None,
                 append: bool = False):
        self.delegation_id = delegation_id
        self.task_index = task_index
        self._ok = False
        self._lock = threading.Lock()
        self._stream_buf: List[str] = []
        self._stream_len = 0
        self._tool_facts: Dict[str, Dict[str, Any]] = {}
        self.path: Optional[Path] = None
        with _best_effort(f"init ({delegation_id} task {task_index})"):
            goal_line = _one_line(goal, _KICKOFF_MAX)
            d = (root if root is not None else live_transcript_root()) / delegation_id
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"task-{task_index}.log"
            header = (
                "=== Hermes subagent live transcript ===\n"
                f"delegation: {delegation_id}   task: {task_index}\n"
                f"goal: {_redact(goal_line)}\n"  # header bypasses event(), so redact here too
                f"started: {time.strftime(_TIME_FMT)}\n"
                "(append-only; streams while the subagent runs — tail -f me)\n"
                + "=" * 40 + "\n")
            if append and path.exists():
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n=== Hermes subagent continuation resumed ===\n")
            else:
                path.write_text(header, encoding="utf-8")
            self.path, self._ok = path, True
            self.event("user", "kickoff: " + goal_line
                       + (f" | context: {_one_line(context, _KICKOFF_MAX)}" if context else ""))

    def event(self, role: str, text: str) -> None:
        """Append one ``HH:MM:SS role | text`` line. Single choke point: every typed
        helper funnels through here so one redaction covers everything."""
        if not self._ok or self.path is None:
            return
        line = f"{time.strftime('%H:%M:%S')} {role:<9}| {_redact(text)}\n"
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:
            self._ok = False
            logger.debug("Live transcript write failed (%s): %s", self.path, exc)

    def _line(self, role: str, text: str, limit: int) -> None:
        if t := _one_line(text, limit):
            self.event(role, t)

    def assistant_text(self, text: str) -> None:
        self._line("assistant", text, _ASSISTANT_MAX)

    def thinking(self, text: str) -> None:
        self._line("think", text, _THINKING_MAX)

    def tool_start(self, name: str, args_preview: Any = None) -> None:
        self.flush_stream()
        self.event("tool", f"-> {name or '?'}({_one_line(args_preview, _ARGS_MAX)})")

    def checkpoint(self, stage: str, details: Any = None) -> None:
        """Persist a compact lifecycle checkpoint beside the transcript.

        Checkpoints are operational metadata, not submission results.  They
        let recovery distinguish a child that died after ``advance`` from one
        that had already started a final browser action.  Values are bounded
        and redacted; no form values, cookies, or page text are persisted.
        """
        stage = str(stage or "").strip().lower()
        if not stage or not self._ok or self.path is None:
            return
        safe_details = _one_line(details, 220) if details is not None else ""
        if stage == "segment_started":
            self._tool_facts.clear()
            _update_manifest_tool_facts(self.delegation_id, self.task_index, {})
        self.event("checkpoint", _joined(stage, safe_details))
        _update_manifest_checkpoint(self.delegation_id, self.task_index, stage, safe_details)

    def _capture_tool_fact(self, tool: str, result: Any, *, is_error: bool) -> bool:
        """Keep a redacted, allowlisted BacklinkHub result for host progress."""
        if is_error or tool not in {
            "backlinkhub_advance_submission_round",
            "backlinkhub_record_submission_result",
        }:
            return False
        payload = result
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
        if not isinstance(payload, dict):
            return False
        envelope = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        if payload.get("success") is False or payload.get("error") or envelope.get("success") is False or envelope.get("error"):
            return False
        scalar_keys = {
            "success", "run_id", "site_id", "work_item_id", "outcome",
            "remaining", "queue_exhausted", "target_reached", "target",
            "target_count", "daily_target", "candidate_bound", "candidate_external_side_effect",
            "platform_id",
        }
        progress_keys = {
            "published", "pending", "pending_review", "submission_unconfirmed",
            "failed_retryable", "failed_final", "remaining", "queue_exhausted",
            "target_reached", "target", "target_count",
        }
        fact = {key: envelope[key] for key in scalar_keys if key in envelope}
        site = envelope.get("site")
        site_id = site.get("site_id") if isinstance(site, dict) else None
        status = envelope.get("status")
        if isinstance(status, dict):
            status_site_id = status.get("site_id")
            if site_id and status_site_id and str(site_id) != str(status_site_id):
                return False
            site_id = site_id or status_site_id
            progress_site_id = status.get("site_progress", {}).get("site_id") if isinstance(status.get("site_progress"), dict) else None
            if site_id and progress_site_id and str(site_id) != str(progress_site_id):
                return False
            fact["site_progress"] = {
                key: status[key] for key in progress_keys | {"daily_target"}
                if key in status
            }
        if site_id:
            fact["site_id"] = site_id
        candidate = envelope.get("candidate")
        if isinstance(candidate, dict) and candidate.get("platform_id"):
            fact["platform_id"] = candidate["platform_id"]
        for key in ("run_id", "site_id"):
            if key not in fact and key in payload:
                fact[key] = payload[key]
        site_progress = envelope.get("site_progress")
        if isinstance(site_progress, dict):
            fact["site_progress"] = {
                key: site_progress[key] for key in progress_keys | {"daily_target"}
                if key in site_progress
            }
        if tool == "backlinkhub_record_submission_result":
            previous = self._tool_facts.get("backlinkhub_advance_submission_round", {})
            fact.setdefault("run_id", previous.get("run_id"))
            fact.setdefault("site_id", previous.get("site_id"))
            previous_item = previous.get("work_item_id")
            if previous_item and fact.get("work_item_id") and str(previous_item) != str(fact["work_item_id"]):
                return False
        if not fact.get("run_id") or not fact.get("site_id"):
            return False
        if tool == "backlinkhub_record_submission_result" and str(
            fact.get("outcome") or ""
        ).strip().lower() not in {
            "published", "pending", "pending_review", "submission_unconfirmed", "attempted_unconfirmed",
            "failed_retryable", "failed_final",
        }:
            return False
        self._tool_facts[tool] = fact
        _update_manifest_tool_facts(self.delegation_id, self.task_index, self._tool_facts)
        return True

    def tool_result(self, name: str, result: Any = None,
                    duration: Any = None, is_error: bool = False) -> None:
        status = "ERROR" if is_error else "ok"
        try:
            dur = "" if duration is None else f" {float(duration):.1f}s"
        except (TypeError, ValueError):
            dur = ""
        self.event("result", f"{name or '?'} {status}{dur}: {_one_line(result, _RESULT_MAX)}")

    def marker(self, text: str) -> None:
        """Lifecycle marker: start / final / error / interrupt / budget."""
        self.flush_stream()
        self.event("final", _one_line(text, _ASSISTANT_MAX))

    def add_stream_delta(self, delta: str) -> None:
        """Buffer streamed assistant reply text; flushed as one line."""
        if not delta or not self._ok:
            return
        self._stream_buf.append(delta)
        self._stream_len += len(delta)
        if self._stream_len >= _STREAM_BUFFER_FLUSH_CHARS:
            self.flush_stream()

    def flush_stream(self) -> None:
        if self._stream_buf:
            text, self._stream_buf, self._stream_len = "".join(self._stream_buf), [], 0
            self.assistant_text(text)

    def _on_complete(self, tool_name, preview, args, kwargs):
        dur = kwargs.get("duration_seconds")
        summary = kwargs.get("summary") or preview
        self.marker(_joined(
            f"status={kwargs.get('status', '?')}",
            f"duration={dur}s" if dur is not None else "",
            f"summary: {_one_line(summary, _RESULT_MAX)}" if summary else ""))

    # Event demux (the tool_progress_callback surface): handler(self, tool_name, preview, args, kwargs).
    _OBSERVERS = {
        "tool.started": lambda s, n, p, a, kw: s.tool_start(str(n or ""), p if p else a),
        "tool.completed": lambda s, n, p, a, kw: s.tool_result(
            str(n or ""), result=kw.get("result"), duration=kw.get("duration"),
            is_error=bool(kw.get("is_error"))),
        # Fired as cb("_thinking", <text>) — text rides in the tool_name slot.
        "_thinking": lambda s, n, p, a, kw: s.thinking(str(n or p or "")),
        # cb("reasoning.available", "_thinking", <text>, None)
        "reasoning.available": lambda s, n, p, a, kw: s.thinking(str(p or "")),
        "subagent.text": lambda s, n, p, a, kw: s.add_stream_delta(str(p or "")),
        "subagent.start": lambda s, n, p, a, kw: s.event("start", _one_line(p, _KICKOFF_MAX)),
        "subagent.complete": _on_complete}

    def observe(self, event_type: Any, tool_name: Any = None, preview: Any = None,
                args: Any = None, **kwargs: Any) -> None:
        """Map a child tool_progress_callback event onto transcript lines.
        Unknown events are ignored. Never raises (event() swallows I/O)."""
        event_name = str(event_type or "")
        tool = str(tool_name or "").strip()
        if event_name == "tool.started":
            if tool == "backlinkhub_advance_submission_round":
                self.checkpoint("advance_started")
            elif tool == "backlinkhub_record_submission_result":
                self.checkpoint("record_started")
            elif tool == "backlinkhub_record_runtime_metric":
                metric = args if isinstance(args, dict) else preview
                stage = _stage_from_runtime_metric(metric)
                if stage:
                    self.checkpoint(stage, metric.get("fingerprint"))
            elif tool == "terminal" and "ego-browser" in str(preview or args or ""):
                self.checkpoint("browser_started")
        elif event_name == "tool.completed":
            ok = not bool(kwargs.get("is_error"))
            if tool == "backlinkhub_advance_submission_round":
                captured = self._capture_tool_fact(tool, kwargs.get("result"), is_error=not ok)
                fact = self._tool_facts.get(tool, {})
                detail = json.dumps({key: fact[key] for key in ("run_id", "site_id", "work_item_id") if fact.get(key)}, sort_keys=True)
                self.checkpoint("advanced" if captured else "advance_failed", detail if captured else None)
            elif tool == "backlinkhub_record_submission_result":
                captured = self._capture_tool_fact(tool, kwargs.get("result"), is_error=not ok)
                fact = self._tool_facts.get(tool, {})
                detail = json.dumps({key: fact[key] for key in ("run_id", "site_id", "work_item_id", "outcome") if fact.get(key)}, sort_keys=True)
                self.checkpoint("recorded" if captured else "record_failed", detail if captured else None)
            elif tool == "backlinkhub_record_runtime_metric" and ok:
                metric = args if isinstance(args, dict) else preview
                stage = _stage_from_runtime_metric(metric)
                if stage:
                    self.checkpoint(stage, metric.get("fingerprint"))
            if tool not in {
                "backlinkhub_advance_submission_round",
                "backlinkhub_record_submission_result",
            }:
                self._capture_tool_fact(tool, kwargs.get("result"), is_error=not ok)
        handler = self._OBSERVERS.get(event_name)
        if handler is not None:
            handler(self, tool_name, preview, args, kwargs)

    def finalize(self, entry: Dict[str, Any]) -> None:
        """Terminal marker with exit-reason detail subagent.complete lacks."""
        exit_reason = entry.get("exit_reason")
        self.marker(_joined(
            f"end status={entry.get('status', '?')}",
            f"exit_reason={exit_reason}" if exit_reason else "",
            "(iteration budget exhausted)" if exit_reason == "max_iterations" else "",
            f"error: {_one_line(entry['error'], _RESULT_MAX)}" if entry.get("error") else ""))


def wrap_progress_callback(inner_cb, writer: LiveTranscriptWriter):
    """Wrap a child's tool_progress_callback (may be None) so events also land in
    the log; writer failures never propagate. Preserves the ``_flush`` contract."""

    def _cb(event_type, tool_name=None, preview=None, args=None, **kwargs):
        with _best_effort("observe"):
            writer.observe(event_type, tool_name, preview, args, **kwargs)
        if inner_cb is not None:
            inner_cb(event_type, tool_name, preview, args, **kwargs)

    def _flush():
        with _best_effort("flush"):
            writer.flush_stream()
        if callable(getattr(inner_cb, "_flush", None)):
            inner_cb._flush()

    _cb._flush = _flush
    return _cb


def create_live_transcripts(
    task_list: List[Dict[str, Any]], context: Optional[str] = None,
    delegation_id: Optional[str] = None, model: Optional[str] = None,
    provider: Optional[str] = None,
) -> tuple[Optional[str], List[Optional[LiveTranscriptWriter]], List[str]]:
    """One pre-headered writer per task + a manifest.json; prunes stale dirs.
    Returns ``(delegation_id, writers, paths)``; on any top-level failure
    ``(None, [None]*n, [])`` so delegation proceeds untouched."""
    n = len(task_list)
    prune_stale_live_dirs()  # best-effort; never raises
    with _best_effort("creation"):
        # Same id shape as async_delegation's so the dir name matches the handle.
        deleg_id = delegation_id or f"deleg_{uuid.uuid4().hex[:8]}"
        existing_manifest = _manifest_path(deleg_id).is_file() if delegation_id else False
        made = [LiveTranscriptWriter(
                    deleg_id, i, str(t.get("goal", "")), context=t.get("context") or context,
                    append=existing_manifest,
                )
                for i, t in enumerate(task_list)]
        writers: List[Optional[LiveTranscriptWriter]] = [w if w.path is not None else None for w in made]
        paths: List[str] = [str(w.path) for w in made if w.path is not None]
        if not paths:
            return None, [None] * n, []
        if not existing_manifest:
            _write_manifest(deleg_id, task_list, paths, model=model, provider=provider)
        else:
            with _MANIFEST_LOCK, _best_effort("continuation manifest resume"):
                manifest_path = _manifest_path(deleg_id)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["status"] = "running"
                manifest.pop("recovery_reason", None)
                manifest.pop("completed", None)
                manifest["updated"] = time.strftime(_TIME_FMT)
                _dump_json(manifest_path, manifest)
        return deleg_id, writers, paths
    return None, [None] * n, []


def _manifest_path(delegation_id: str) -> Path:
    return live_transcript_root() / delegation_id / "manifest.json"


def _write_manifest(delegation_id: str, task_list: List[Dict[str, Any]],
                    paths: List[str], model: Optional[str] = None,
                    provider: Optional[str] = None) -> None:
    with _best_effort("manifest write"):
        with _MANIFEST_LOCK:
            _dump_json(_manifest_path(delegation_id), {
                "delegation_id": delegation_id, "started": time.strftime(_TIME_FMT),
                "task_count": len(task_list), "model": model, "provider": provider,
                "tasks": [{
                    "index": i,
                    # Same mounted dir as the .log files, so the goal needs the same redaction.
                    "goal": _redact(str(t.get("goal", ""))[:500]),
                    "log": paths[i] if i < len(paths) else None,
                    "status": "running"} for i, t in enumerate(task_list)]})


def update_manifest_statuses(delegation_id: Optional[str],
                             results: List[Dict[str, Any]]) -> None:
    """Best-effort per-task status update once the batch has aggregated."""
    if not delegation_id:
        return
    with _best_effort("manifest update"):
        with _MANIFEST_LOCK:
            mp = _manifest_path(delegation_id)
            manifest = json.loads(mp.read_text(encoding="utf-8"))
            by_index = {r.get("task_index"): r for r in results if isinstance(r, dict)}
            for task in manifest.get("tasks", []):
                r = by_index.get(task.get("index"))
                if r is not None:
                    task["status"] = r.get("status", task.get("status"))
                    if r.get("exit_reason"):
                        task["exit_reason"] = r["exit_reason"]
            manifest["completed"] = time.strftime(_TIME_FMT)
            _dump_json(mp, manifest)


def prune_stale_live_dirs(max_age_days: int = LIVE_RETENTION_DAYS) -> int:
    """Remove live/<delegation_id> dirs older than the retention window. Best-effort."""
    removed = 0
    with _best_effort("pruning"):
        root = live_transcript_root()
        if not root.is_dir():
            return 0
        cutoff = time.time() - max_age_days * 86400
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

def new_live_delegation_id() -> str:
    """Same shape as async_delegation's ids so the dir name matches the handle."""
    return f"deleg_{uuid.uuid4().hex[:8]}"
# ---- END PLUGIN-COMPAT ----
