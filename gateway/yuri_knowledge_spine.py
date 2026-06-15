"""Small append-only knowledge spine for Yuri front-desk work.

The spine is deliberately file-backed and prompt-facing:

* ``events.jsonl`` keeps an auditable trail of intakes and review outcomes.
* A compact context pack is injected into Yuri Kanban root cards so planners,
  workers, and reviewers start from the same operating facts.

This is not model training. It is structured recall plus automatic injection.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "yuri-knowledge-spine-v1"

OPERATING_RULES = [
    "Yuri is the front-desk chief of staff, not the sole worker.",
    "Actionable work should flow through a planner/root Kanban card unless it is a narrow direct fact answer.",
    "Workers must use current evidence and source-of-truth checks instead of stale memory or guesses.",
    "User-facing completion must be a reviewed result, not internal routing chatter.",
    "If intent, evidence, or deliverables are incomplete, hold the final report and state the missing proof.",
]

REVIEW_CONTRACT = {
    "required_review_status": "pass",
    "accepted_intent_sources": ["telethon", "telegram-safe", "telegram", "conversation"],
    "approved_text_keys": ["approved_final_text", "final_text", "approved_text"],
}


def _spine_root() -> Path:
    env_path = os.getenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state" / "yuri_knowledge_spine"


def _events_path() -> Path:
    return _spine_root() / "events.jsonl"


def _db_path() -> Path:
    return _spine_root() / "spine.db"


def _utc_iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def _safe_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 18].rstrip() + " ... [truncated]"


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return str(value)


def _payload_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "original_user_text",
        "approved_final_text",
        "intent_source",
        "board",
        "task_id",
        "root_task_id",
        "reviewer_task_id",
    ):
        val = payload.get(key)
        if val:
            parts.append(str(val))
    for key in ("recent_spine_events", "operating_rules"):
        val = payload.get(key)
        if isinstance(val, list):
            parts.extend(str(item) for item in val if item)
    return _safe_text("\n".join(parts), limit=6000)


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _event_task_id(payload: dict[str, Any]) -> str:
    return str(payload.get("task_id") or payload.get("root_task_id") or "")


def _ensure_db(conn: sqlite3.Connection) -> bool:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            task_id TEXT,
            root_task_id TEXT,
            reviewer_task_id TEXT,
            intent_source TEXT,
            board TEXT,
            text TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    fts_enabled = True
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
            USING fts5(event_id UNINDEXED, kind, task_id, text)
            """
        )
    except sqlite3.Error:
        fts_enabled = False
    conn.commit()
    return fts_enabled


def _index_event(row: dict[str, Any]) -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _event_payload(row)
    text = _payload_text(payload)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(str(path), timeout=2) as conn:
        fts_enabled = _ensure_db(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO events (
                event_id, created_at, kind, task_id, root_task_id,
                reviewer_task_id, intent_source, board, text, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("event_id"),
                row.get("created_at"),
                row.get("kind"),
                payload.get("task_id"),
                payload.get("root_task_id"),
                payload.get("reviewer_task_id"),
                payload.get("intent_source"),
                payload.get("board"),
                text,
                payload_json,
            ),
        )
        if fts_enabled:
            conn.execute("DELETE FROM events_fts WHERE event_id = ?", (row.get("event_id"),))
            conn.execute(
                "INSERT INTO events_fts(event_id, kind, task_id, text) VALUES (?, ?, ?, ?)",
                (
                    row.get("event_id"),
                    row.get("kind"),
                    _event_task_id(payload),
                    text,
                ),
            )
        conn.commit()


def append_event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "created_at": _utc_iso(),
        "kind": str(kind or "").strip(),
        "payload": _jsonable(payload),
    }
    path = _events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    try:
        _index_event(row)
    except Exception:
        pass
    return row


def recent_events(limit: int = 6) -> list[dict[str, Any]]:
    path = _events_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(0, int(limit)) :]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _all_events_from_jsonl(limit: int = 200) -> list[dict[str, Any]]:
    path = _events_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(0, int(limit)) :]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


_TOKEN_RE = re.compile(r"[A-Za-z0-9_가-힣]{2,}")


def _tokens(text: str) -> set[str]:
    return {m.group(0).casefold() for m in _TOKEN_RE.finditer(text or "")}


def _fts_query(text: str) -> str:
    toks = sorted(_tokens(text))
    return " OR ".join(toks[:12])


def _row_from_index(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": row["event_id"],
        "created_at": row["created_at"],
        "kind": row["kind"],
        "payload": payload,
    }


def recall_relevant_events(query: str, *, limit: int = 4) -> list[dict[str, Any]]:
    """Return events most relevant to ``query``.

    FTS5 is used when available. If SQLite indexing is unavailable or stale,
    fall back to bounded JSONL token-overlap scoring.
    """
    query = _safe_text(query, limit=1000)
    if not query:
        return []
    db = _db_path()
    fts = _fts_query(query)
    if db.is_file() and fts:
        try:
            with sqlite3.connect(str(db), timeout=2) as conn:
                conn.row_factory = sqlite3.Row
                _ensure_db(conn)
                rows = conn.execute(
                    """
                    SELECT e.*
                      FROM events_fts f
                      JOIN events e ON e.event_id = f.event_id
                     WHERE events_fts MATCH ?
                     ORDER BY bm25(events_fts), e.created_at DESC
                     LIMIT ?
                    """,
                    (fts, int(limit)),
                ).fetchall()
                if rows:
                    return [_row_from_index(row) for row in rows]
        except sqlite3.Error:
            pass

    q_tokens = _tokens(query)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for row in _all_events_from_jsonl(limit=240):
        payload = _event_payload(row)
        text = _payload_text(payload)
        score = len(q_tokens & _tokens(text))
        if score:
            scored.append((score, str(row.get("created_at") or ""), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in scored[: int(limit)]]


def _summarize_event(row: dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    kind = str(row.get("kind") or "")
    created_at = str(row.get("created_at") or "")
    if kind == "yuri_intake":
        task_id = payload.get("task_id") or "unassigned"
        text = _safe_text(payload.get("original_user_text"), limit=140)
        return f"{created_at} intake {task_id}: {text}"
    if kind == "review_finalized":
        task_id = payload.get("root_task_id") or "unknown"
        source = payload.get("intent_source") or "unknown"
        text = _safe_text(payload.get("approved_final_text"), limit=140)
        return f"{created_at} review pass {task_id} source={source}: {text}"
    return f"{created_at} {kind}"


def build_context_pack(
    *,
    original_user_text: str,
    platform: str = "telegram",
    message_id: Optional[str] = None,
    task_id: Optional[str] = None,
    recent_limit: int = 4,
    relevant_limit: int = 4,
) -> dict[str, Any]:
    relevant = recall_relevant_events(original_user_text, limit=relevant_limit)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_iso(),
        "source": {
            "platform": _safe_text(platform, limit=80),
            "message_id": _safe_text(message_id, limit=120) if message_id else None,
        },
        "task_id": task_id,
        "original_user_text": _safe_text(original_user_text),
        "operating_rules": list(OPERATING_RULES),
        "review_contract": dict(REVIEW_CONTRACT),
        "relevant_spine_events": [
            _summarize_event(row) for row in relevant
        ],
        "recent_spine_events": [
            _summarize_event(row) for row in recent_events(limit=recent_limit)
        ],
    }


def render_context_pack(pack: dict[str, Any]) -> str:
    source = pack.get("source") if isinstance(pack.get("source"), dict) else {}
    review = pack.get("review_contract") if isinstance(pack.get("review_contract"), dict) else {}
    lines = [
        "YURI KNOWLEDGE SPINE CONTEXT PACK (auto-injected, not user-facing):",
        f"- schema_version: {pack.get('schema_version') or SCHEMA_VERSION}",
        f"- source: {source.get('platform') or 'unknown'} message_id={source.get('message_id') or 'unknown'}",
        "- original_user_text:",
        _safe_text(pack.get("original_user_text"), limit=2000),
        "- operating_rules:",
    ]
    for rule in pack.get("operating_rules") or []:
        lines.append(f"  - {rule}")
    lines.extend(
        [
            "- review_contract:",
            f"  - required_review_status: {review.get('required_review_status') or 'pass'}",
            "  - accepted_intent_sources: "
            + ", ".join(str(v) for v in (review.get("accepted_intent_sources") or [])),
            "  - approved_text_keys: "
            + ", ".join(str(v) for v in (review.get("approved_text_keys") or [])),
        ]
    )
    recent = pack.get("recent_spine_events") or []
    relevant = pack.get("relevant_spine_events") or []
    if relevant:
        lines.append("- relevant_spine_events:")
        for item in relevant:
            lines.append(f"  - {_safe_text(item, limit=260)}")
    if recent:
        lines.append("- recent_spine_events:")
        for item in recent:
            lines.append(f"  - {_safe_text(item, limit=240)}")
    return "\n".join(lines)


def record_intake(pack: dict[str, Any], *, task_id: str) -> dict[str, Any]:
    payload = dict(pack)
    payload["task_id"] = task_id
    return append_event("yuri_intake", payload)


def record_review_result(
    *,
    root_task_id: str,
    reviewer_task_id: str,
    approved_final_text: str,
    intent_source: str,
    board: Optional[str] = None,
) -> dict[str, Any]:
    return append_event(
        "review_finalized",
        {
            "root_task_id": root_task_id,
            "reviewer_task_id": reviewer_task_id,
            "approved_final_text": _safe_text(approved_final_text),
            "intent_source": _safe_text(intent_source, limit=120),
            "board": _safe_text(board, limit=120) if board else None,
        },
    )


def _format_payload_field(value: Any, *, limit: int = 220) -> str:
    if value is None:
        return ""
    return _safe_text(value, limit=limit).replace("\n", " / ")


def _find_kanban_task(task_id: str) -> Optional[dict[str, Any]]:
    if not task_id:
        return None
    try:
        from hermes_cli import kanban_db as kb

        try:
            boards = kb.list_boards(include_archived=False)
        except Exception:
            boards = [kb.read_board_metadata(kb.DEFAULT_BOARD)]
        for meta in boards:
            board = meta.get("slug") or kb.DEFAULT_BOARD
            try:
                conn = kb.connect(board=board)
            except Exception:
                continue
            try:
                task = kb.get_task(conn, task_id)
                if not task:
                    continue
                runs = kb.list_runs(conn, task_id)
                events = kb.list_events(conn, task_id)
                comments = kb.list_comments(conn, task_id)
                return {
                    "board": board,
                    "task": task,
                    "runs": runs,
                    "events": events,
                    "comments": comments,
                }
            finally:
                conn.close()
    except Exception:
        return None
    return None


def _task_line(task_info: dict[str, Any]) -> str:
    task = task_info["task"]
    return (
        f"{task.id} board={task_info['board']} status={task.status} "
        f"assignee={task.assignee or '-'} title={_format_payload_field(task.title, limit=120)}"
    )


def _run_line(run: Any) -> str:
    meta = getattr(run, "metadata", None) or {}
    bits = [
        f"run#{getattr(run, 'id', '?')}",
        f"outcome={getattr(run, 'outcome', None) or getattr(run, 'status', None) or '-'}",
    ]
    if isinstance(meta, dict):
        status = meta.get("review_status")
        source = meta.get("intent_source")
        reviewer = meta.get("reviewer_task")
        if status:
            bits.append(f"review_status={status}")
        if source:
            bits.append(f"intent_source={source}")
        if reviewer:
            bits.append(f"reviewer={reviewer}")
    summary = _format_payload_field(getattr(run, "summary", ""), limit=220)
    if summary:
        bits.append(f"summary={summary}")
    return " ".join(bits)


def build_audit_report(query: str, *, limit: int = 5) -> str:
    """Return a compact diagnostic report for a Yuri memory/task question."""
    query = _safe_text(query, limit=500)
    lines = [
        "Yuri memory audit",
        f"- query: {query or '(empty)'}",
        f"- spine_root: {_spine_root()}",
    ]
    direct_task = query if re.fullmatch(r"t_[0-9a-fA-F]+", query or "") else ""
    events = recall_relevant_events(query, limit=limit) if query else recent_events(limit=limit)
    seen_task_ids: list[str] = []
    if direct_task:
        seen_task_ids.append(direct_task)

    if events:
        lines.append("- relevant_spine_events:")
        for row in events:
            payload = _event_payload(row)
            task_id = _event_task_id(payload)
            if task_id and task_id not in seen_task_ids:
                seen_task_ids.append(task_id)
            lines.append(f"  - {_summarize_event(row)}")
    else:
        lines.append("- relevant_spine_events: none")

    if not seen_task_ids:
        lines.append("- kanban: no task ids found from query/spine")
    else:
        lines.append("- kanban_trace:")
        for task_id in seen_task_ids[:limit]:
            info = _find_kanban_task(task_id)
            if not info:
                lines.append(f"  - {task_id}: not found on active boards")
                continue
            lines.append(f"  - {_task_line(info)}")
            runs = info.get("runs") or []
            if runs:
                for run in runs[-3:]:
                    lines.append(f"    - {_run_line(run)}")
            events_tail = info.get("events") or []
            if events_tail:
                event_bits = [
                    f"{getattr(ev, 'kind', '-')}"
                    for ev in events_tail[-5:]
                ]
                lines.append(f"    - events_tail: {', '.join(event_bits)}")
            comments = info.get("comments") or []
            if comments:
                last = comments[-1]
                lines.append(
                    "    - last_comment: "
                    f"{getattr(last, 'author', '-')}: "
                    f"{_format_payload_field(getattr(last, 'body', ''), limit=180)}"
                )

    lines.append("- recommendation: use this audit to verify whether Yuri used the right Telegram intent, spine facts, kanban result, and reviewer pass before final reporting.")
    return "\n".join(lines)
