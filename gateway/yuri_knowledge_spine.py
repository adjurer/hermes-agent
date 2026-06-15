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
) -> dict[str, Any]:
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
