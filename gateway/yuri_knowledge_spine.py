"""Small append-only knowledge spine for Yuri front-desk work.

The spine is deliberately file-backed and prompt-facing:

* ``events.jsonl`` keeps an auditable trail of intakes and review outcomes.
* A compact context pack is injected into Yuri Kanban root cards so planners,
  workers, and reviewers start from the same operating facts.

This is not model training. It is structured recall plus automatic injection.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "yuri-knowledge-spine-v1"
GRAPH_SCHEMA_VERSION = "yuri-knowledge-graph-v1"
OKF_VERSION = "0.1"
LEARNING_SCHEMA_VERSION = "yuri-learning-v1"
GRAPHITI_EPISODE_SCHEMA_VERSION = "yuri-graphiti-episodes-v1"
FAILURE_SCHEMA_VERSION = "yuri-failure-case-v1"
REGRESSION_SCHEMA_VERSION = "yuri-regression-candidate-v1"

OPERATING_RULES = [
    "Yuri is the front-desk chief of staff, not the sole worker.",
    "Actionable work should flow through a planner/root Kanban card unless it is a narrow direct fact answer.",
    "Workers must use current evidence and source-of-truth checks instead of stale memory or guesses.",
    "User-facing completion must be a reviewed result, not internal routing chatter.",
    "If intent, evidence, or deliverables are incomplete, hold the final report and state the missing proof.",
    "If a final report mentions a file, attachment, document, or filename, the native attachment must be available and delivered; otherwise hold for rework.",
    "For codebase reading, bug investigation, tests, refactoring, repository structure, or PR review, workers may use /Users/tbd/.hermes/bin/hermes-gajae-run; do not use Gajae-Code for non-development research, Yuri replies, or final reports.",
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


def _graph_edges_path() -> Path:
    return _spine_root() / "graph_edges.jsonl"


def _okf_bundle_path() -> Path:
    return _spine_root() / "okf_bundle"


def _graphiti_episodes_path() -> Path:
    return _spine_root() / "graphiti_episodes.jsonl"


def _lessons_path() -> Path:
    return _spine_root() / "lessons.jsonl"


def _failures_path() -> Path:
    return _spine_root() / "failures.jsonl"


def _regression_candidates_path() -> Path:
    return _spine_root() / "regression_candidates.jsonl"


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_edges (
            edge_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            relation TEXT NOT NULL,
            target TEXT NOT NULL,
            properties_json TEXT NOT NULL
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


_NODE_RE = re.compile(r"[^A-Za-z0-9_가-힣.-]+")


def _stable_hash(*parts: Any, length: int = 16) -> str:
    raw = json.dumps(_jsonable(parts), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _node(kind: str, value: Any) -> str:
    text = _safe_text(value, limit=160)
    if not text:
        text = "unknown"
    slug = _NODE_RE.sub("_", text).strip("_").casefold()
    if len(slug) > 80:
        slug = f"{slug[:48]}_{_stable_hash(text, length=12)}"
    return f"{kind}:{slug or 'unknown'}"


def _graph_edge(
    *,
    row: dict[str, Any],
    source: str,
    relation: str,
    target: str,
    properties: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "edge_id": _stable_hash(row.get("event_id"), source, relation, target),
        "source": source,
        "relation": relation,
        "target": target,
        "created_at": row.get("created_at"),
        "event_id": row.get("event_id"),
        "event_kind": row.get("kind"),
        "properties": _jsonable(properties or {}),
    }


def _project_graph_edges(row: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _event_payload(row)
    kind = str(row.get("kind") or "")
    edges: list[dict[str, Any]] = []

    if kind == "yuri_intake":
        task_id = payload.get("task_id") or row.get("event_id")
        task = _node("task", task_id)
        text = _safe_text(payload.get("original_user_text"), limit=1200)
        if text:
            intent = _node("intent", _stable_hash(text, length=20))
            edges.append(
                _graph_edge(
                    row=row,
                    source=task,
                    relation="HAS_USER_INTENT",
                    target=intent,
                    properties={"task_id": task_id, "text": text},
                )
            )
        review = payload.get("review_contract")
        if not isinstance(review, dict):
            review = REVIEW_CONTRACT
        required_status = review.get("required_review_status") or "pass"
        edges.append(
            _graph_edge(
                row=row,
                source=task,
                relation="REQUIRES_REVIEW_STATUS",
                target=_node("review_status", required_status),
                properties={"task_id": task_id},
            )
        )
        for source_name in review.get("accepted_intent_sources") or []:
            edges.append(
                _graph_edge(
                    row=row,
                    source=task,
                    relation="ACCEPTS_INTENT_SOURCE",
                    target=_node("intent_source", source_name),
                    properties={"task_id": task_id},
                )
            )
        source_info = payload.get("source")
        if isinstance(source_info, dict) and source_info.get("platform"):
            edges.append(
                _graph_edge(
                    row=row,
                    source=task,
                    relation="ROUTED_FROM",
                    target=_node("platform", source_info.get("platform")),
                    properties={
                        "task_id": task_id,
                        "message_id": source_info.get("message_id"),
                    },
                )
            )

    elif kind == "review_finalized":
        root_task_id = payload.get("root_task_id") or row.get("event_id")
        reviewer_task_id = payload.get("reviewer_task_id")
        task = _node("task", root_task_id)
        if reviewer_task_id:
            edges.append(
                _graph_edge(
                    row=row,
                    source=task,
                    relation="REVIEWED_BY",
                    target=_node("task", reviewer_task_id),
                    properties={
                        "root_task_id": root_task_id,
                        "reviewer_task_id": reviewer_task_id,
                    },
                )
            )
        approved_text = _safe_text(payload.get("approved_final_text"), limit=1200)
        if approved_text:
            edges.append(
                _graph_edge(
                    row=row,
                    source=task,
                    relation="APPROVED_WITH",
                    target=_node("approval", _stable_hash(approved_text, length=20)),
                    properties={
                        "root_task_id": root_task_id,
                        "approved_final_text": approved_text,
                    },
                )
            )
        if payload.get("intent_source"):
            edges.append(
                _graph_edge(
                    row=row,
                    source=task,
                    relation="INTENT_SOURCE",
                    target=_node("intent_source", payload.get("intent_source")),
                    properties={"root_task_id": root_task_id},
                )
            )
        if payload.get("board"):
            edges.append(
                _graph_edge(
                    row=row,
                    source=task,
                    relation="ON_BOARD",
                    target=_node("board", payload.get("board")),
                    properties={"root_task_id": root_task_id},
                )
            )

    return edges


def _append_graph_edges(row: dict[str, Any]) -> None:
    edges = _project_graph_edges(row)
    if not edges:
        return
    path = _graph_edges_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for edge in edges:
            f.write(json.dumps(edge, ensure_ascii=False, sort_keys=True) + "\n")

    db = _db_path()
    with sqlite3.connect(str(db), timeout=2) as conn:
        _ensure_db(conn)
        for edge in edges:
            conn.execute(
                """
                INSERT OR REPLACE INTO graph_edges (
                    edge_id, event_id, created_at, source, relation, target, properties_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.get("edge_id"),
                    edge.get("event_id"),
                    edge.get("created_at"),
                    edge.get("source"),
                    edge.get("relation"),
                    edge.get("target"),
                    json.dumps(edge.get("properties") or {}, ensure_ascii=False, sort_keys=True),
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
    try:
        _append_graph_edges(row)
    except Exception:
        pass
    try:
        _derive_learning_from_event(row)
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


def recent_graph_edges(limit: int = 50) -> list[dict[str, Any]]:
    path = _graph_edges_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(0, int(limit)) :]:
        try:
            edge = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(edge, dict):
            out.append(edge)
    return out


_TOKEN_RE = re.compile(r"[A-Za-z0-9_가-힣]{2,}")
_STOP_TOKENS = {
    "그리고",
    "그러면",
    "그럼",
    "다시",
    "한번",
    "확인",
    "해주세요",
    "진행",
    "적용",
    "유리",
    "텔레쏜",
    "코덱스",
    "hermes",
    "yuri",
}


def _tokens(text: str) -> set[str]:
    return {m.group(0).casefold() for m in _TOKEN_RE.finditer(text or "")}


def _trigger_terms(text: str, *, limit: int = 12) -> list[str]:
    terms = [tok for tok in _tokens(text) if tok not in _STOP_TOKENS]
    terms.sort(key=lambda tok: (-len(tok), tok))
    return terms[:limit]


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


def _find_intake_event_for_task(task_id: str) -> Optional[dict[str, Any]]:
    if not task_id:
        return None
    for row in reversed(_all_events_from_jsonl(limit=1000)):
        if row.get("kind") != "yuri_intake":
            continue
        payload = _event_payload(row)
        if str(payload.get("task_id") or "") == task_id:
            return row
    return None


def _lesson_text(lesson: dict[str, Any]) -> str:
    parts = [
        lesson.get("user_intent"),
        lesson.get("approved_final_text"),
        lesson.get("learning_rule"),
        " ".join(str(v) for v in lesson.get("trigger_terms") or []),
    ]
    return _safe_text("\n".join(str(part) for part in parts if part), limit=6000)


def _lesson_from_review(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    payload = _event_payload(row)
    root_task_id = _safe_text(payload.get("root_task_id"), limit=120)
    approved_text = _safe_text(payload.get("approved_final_text"), limit=2400)
    if not root_task_id or not approved_text:
        return None
    intake = _find_intake_event_for_task(root_task_id)
    intake_payload = _event_payload(intake or {})
    user_intent = _safe_text(intake_payload.get("original_user_text"), limit=2000)
    trigger_source = "\n".join([user_intent, approved_text])
    return {
        "schema_version": LEARNING_SCHEMA_VERSION,
        "lesson_id": _stable_hash(
            root_task_id,
            payload.get("reviewer_task_id"),
            approved_text,
            length=20,
        ),
        "created_at": _utc_iso(),
        "source_event_id": row.get("event_id"),
        "root_task_id": root_task_id,
        "reviewer_task_id": _safe_text(payload.get("reviewer_task_id"), limit=120),
        "intent_source": _safe_text(payload.get("intent_source"), limit=120),
        "board": _safe_text(payload.get("board"), limit=120),
        "kind": "review_pass_lesson",
        "trigger_terms": _trigger_terms(trigger_source),
        "user_intent": user_intent,
        "approved_final_text": approved_text,
        "learning_rule": (
            "Require review_status=pass before user-facing report. "
            "For similar Yuri front-desk requests, preserve the Telegram intent, "
            "route actionable work through planner/office flow, and report only "
            "the reviewer-approved final result. If the final result claims a "
            "file or attachment, verify that the native attachment is available "
            "and delivered before sending the completion report."
        ),
    }


def _read_lessons(limit: int = 200) -> list[dict[str, Any]]:
    path = _lessons_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        lesson_id = str(row.get("lesson_id") or "")
        if lesson_id and lesson_id in seen:
            continue
        if lesson_id:
            seen.add(lesson_id)
        out.append(row)
        if len(out) >= max(0, int(limit)):
            break
    out.reverse()
    return out


def _append_lesson(lesson: dict[str, Any]) -> None:
    path = _lessons_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_jsonable(lesson), ensure_ascii=False, sort_keys=True) + "\n")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl_unique(path: Path, *, id_key: str, limit: int = 200) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        row_id = str(row.get(id_key) or "")
        if row_id and row_id in seen:
            continue
        if row_id:
            seen.add(row_id)
        out.append(row)
        if len(out) >= max(0, int(limit)):
            break
    out.reverse()
    return out


_FAILURE_HINTS = {
    "file_claim_without_media": {
        "root_cause": "파일 전송 완료 표현이 실제 첨부 evidence보다 먼저 나갔습니다.",
        "expected_behavior": "전송 API 결과나 media/document evidence가 없으면 전송 완료라고 말하지 않습니다.",
        "forbidden_behavior": "파일을 보냈다고 말하고 실제 첨부를 누락하는 것.",
        "test_prompt": "파일 보냈다고 보고하기 전에 실제 첨부 확인이 필요한가요?",
    },
    "linebreak_or_readability": {
        "root_cause": "긴 Telegram 보고가 문단/목록으로 나뉘지 않아 읽기 어렵습니다.",
        "expected_behavior": "최종 보고는 짧은 문단과 목록으로 줄바꿈해서 보냅니다.",
        "forbidden_behavior": "긴 한 문단으로 보고하거나 줄바꿈 적용을 확인 없이 주장하는 것.",
        "test_prompt": "보고 문장 줄바꿈 규칙이 적용됐나요?",
    },
    "misunderstood_intent": {
        "root_cause": "최근 Telegram/Telethon 의도 확인 없이 오래된 기억이나 일반 라우팅으로 답했습니다.",
        "expected_behavior": "유리 오해/말귀/답답함 지적은 최신 대화와 live config/code evidence를 먼저 봅니다.",
        "forbidden_behavior": "설정했다거나 확인했다고 말하면서 실제 변경/readback 증거를 남기지 않는 것.",
        "test_prompt": "유리가 말귀를 못 알아듣는다는 지적은 어떤 순서로 확인해야 하나요?",
    },
    "completion_without_review": {
        "root_cause": "검수 통과와 승인된 최종문안 없이 완료 보고가 나가려 했습니다.",
        "expected_behavior": "review_status=pass, intent_source, approved_final_text가 있어야 최종 보고합니다.",
        "forbidden_behavior": "검수/승인 문안 없이 마무리 또는 완료라고 보고하는 것.",
        "test_prompt": "완료 보고 전에 필요한 검수 metadata는 무엇인가요?",
    },
    "data_trust_insufficient": {
        "root_cause": "데이터/파일 결과의 원천, 행수, readback, 검증 명령, 잔여 리스크가 부족했습니다.",
        "expected_behavior": "데이터 산출물은 원천·count·sample/readback·검증·리스크 증거를 갖춘 뒤 보고합니다.",
        "forbidden_behavior": "증거 없이 완료/확보/전수 확인을 말하는 것.",
        "test_prompt": "데이터 결과를 완료 보고하려면 어떤 증거가 필요한가요?",
    },
    "overrouting_simple_question": {
        "root_cause": "간단한 상태/설정/기억 질문을 작업 라우팅으로 넘겼습니다.",
        "expected_behavior": "짧은 사실·상태·진단 질문은 유리가 직접 답하고 실제 변경 지시만 작업으로 보냅니다.",
        "forbidden_behavior": "바로 답할 수 있는 질문을 기획실/칸반으로 넘기는 것.",
        "test_prompt": "기억 설정은 뭐가 있냐는 질문은 바로 답해야 하나요?",
    },
    "ai_processing_source_misrouted": {
        "root_cause": "AI 처리 출처 질문을 '진행중' 단어 때문에 현재 작업 상태 질문으로 오분류했습니다.",
        "expected_behavior": "로컬/GPT/모델/AI 처리 출처 질문은 직전 파이프라인 맥락을 유지하고, 현재 증거를 확인하거나 작업으로 라우팅합니다.",
        "forbidden_behavior": "AI 처리 출처 질문에 '지금 실제로 진행 중인 작업은 없습니다' 같은 칸반 상태로 답하는 것.",
        "test_prompt": "ai 처리가 로컬로 돌고있니? 아님 내 gpt로 진행중이니?",
    },
    "context_drift_followup": {
        "root_cause": "후속 질문을 직전 주제 안에서 해석하지 않고 전체 운영 개선처럼 범위를 넓혔습니다.",
        "expected_behavior": "사용자가 '또', '그럼', '이제', '더'처럼 이어 말하면 직전 주제를 기본 범위로 삼고, 범위를 넓힐 때만 명시합니다.",
        "forbidden_behavior": "직전 주제가 줄바꿈/문체인데 갑자기 전체 시스템 개선 목록으로 확장하는 것.",
        "test_prompt": "줄바꿈 조정 직후 '또 개선할 게 뭐가 있을까?'라고 하면 어느 범위로 답해야 하나요?",
    },
}


def _classify_failure_case(
    user_text: str,
    observed_behavior: str = "",
    issue_code: str = "",
) -> str:
    text = f"{user_text}\n{observed_behavior}\n{issue_code}"
    if issue_code in _FAILURE_HINTS:
        return issue_code
    if re.search(r"(?:파일|첨부|전송|보냈).{0,30}(?:못|누락|안\s*보|없)", text):
        return "file_claim_without_media"
    if re.search(
        r"(?:ai|gpt|모델|로컬|local).{0,50}(?:처리|분류|돌|진행|쓰고|사용)|"
        r"(?:처리|분류).{0,50}(?:로컬|local|gpt|모델|내\s*gpt)",
        text,
        re.IGNORECASE,
    ):
        return "ai_processing_source_misrouted"
    if re.search(r"(?:맥락|문맥|직전|방금|후속|follow.?up|벗어나|이탈|넓혔|확장)", text, re.IGNORECASE):
        return "context_drift_followup"
    if re.search(r"(?:줄바꿈|가독성|읽기|문단)", text):
        return "linebreak_or_readability"
    if re.search(r"(?:말귀|못알아|못\s*알아|답답|멍청|이상한|헛소리|왜\s*그래)", text):
        return "misunderstood_intent"
    if re.search(r"(?:완료|마무리|보고).{0,30}(?:보류|검수|재검수|승인)", text):
        return "completion_without_review"
    if re.search(r"(?:데이터|자료|파일|행수|row|count|누락|전수|검증|신뢰도)", text):
        return "data_trust_insufficient"
    if re.search(r"(?:넘기|라우팅|칸반|기획실).{0,30}(?:인사|간단|기억|설정|상태)", text):
        return "overrouting_simple_question"
    return "misunderstood_intent"


def _failure_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("user_text"),
        row.get("observed_behavior"),
        row.get("root_cause"),
        row.get("expected_behavior"),
        row.get("forbidden_behavior"),
        row.get("category"),
        " ".join(str(v) for v in row.get("trigger_terms") or []),
    ]
    return _safe_text("\n".join(str(part) for part in parts if part), limit=6000)


def _summarize_failure_case(row: dict[str, Any]) -> str:
    category = row.get("category") or "failure"
    text = _safe_text(row.get("user_text"), limit=100)
    expected = _safe_text(row.get("expected_behavior"), limit=140)
    return f"{row.get('created_at') or ''} failure {category}: {text} expected={expected}"


def _regression_candidate_from_failure(row: dict[str, Any]) -> dict[str, Any]:
    category = str(row.get("category") or "misunderstood_intent")
    prompt = str(row.get("test_prompt") or _FAILURE_HINTS.get(category, {}).get("test_prompt") or row.get("user_text") or "")
    return {
        "schema_version": REGRESSION_SCHEMA_VERSION,
        "candidate_id": _stable_hash(row.get("failure_id"), prompt, length=20),
        "created_at": _utc_iso(),
        "source_failure_id": row.get("failure_id"),
        "category": category,
        "prompt": _safe_text(prompt, limit=300),
        "expected_behavior": _safe_text(row.get("expected_behavior"), limit=600),
        "forbidden_behavior": _safe_text(row.get("forbidden_behavior"), limit=600),
        "suggested_test_name": f"test_yuri_{re.sub(r'[^a-z0-9_]+', '_', category.lower()).strip('_')}",
        "status": "candidate",
    }


def record_failure_case(
    *,
    user_text: str,
    observed_behavior: str = "",
    source: str = "manual",
    issue_code: str = "",
    task_id: Optional[str] = None,
    message_id: Optional[str] = None,
    root_cause: str = "",
    expected_behavior: str = "",
    forbidden_behavior: str = "",
    test_prompt: str = "",
) -> dict[str, Any]:
    """Record a Yuri failure as a durable lesson source and regression seed."""
    category = _classify_failure_case(user_text, observed_behavior, issue_code)
    defaults = _FAILURE_HINTS.get(category, {})
    root = _safe_text(root_cause or defaults.get("root_cause") or observed_behavior, limit=800)
    expected = _safe_text(expected_behavior or defaults.get("expected_behavior") or "", limit=800)
    forbidden = _safe_text(forbidden_behavior or defaults.get("forbidden_behavior") or "", limit=800)
    prompt = _safe_text(test_prompt or defaults.get("test_prompt") or user_text, limit=300)
    row = {
        "schema_version": FAILURE_SCHEMA_VERSION,
        "failure_id": _stable_hash(
            category,
            source,
            user_text,
            observed_behavior,
            task_id,
            message_id,
            length=20,
        ),
        "created_at": _utc_iso(),
        "source": _safe_text(source, limit=120),
        "category": category,
        "issue_code": _safe_text(issue_code or category, limit=120),
        "task_id": _safe_text(task_id, limit=120) if task_id else "",
        "message_id": _safe_text(message_id, limit=120) if message_id else "",
        "trigger_terms": _trigger_terms("\n".join([user_text, observed_behavior, category])),
        "user_text": _safe_text(user_text, limit=2000),
        "observed_behavior": _safe_text(observed_behavior, limit=2000),
        "root_cause": root,
        "expected_behavior": expected,
        "forbidden_behavior": forbidden,
        "test_prompt": prompt,
    }
    _append_jsonl(_failures_path(), row)
    _append_jsonl(_regression_candidates_path(), _regression_candidate_from_failure(row))
    return row


def _read_failure_cases(limit: int = 200) -> list[dict[str, Any]]:
    return _read_jsonl_unique(_failures_path(), id_key="failure_id", limit=limit)


def _read_regression_candidates(limit: int = 200) -> list[dict[str, Any]]:
    return _read_jsonl_unique(_regression_candidates_path(), id_key="candidate_id", limit=limit)


def recall_failure_cases(query: str, *, limit: int = 4) -> list[dict[str, Any]]:
    query = _safe_text(query, limit=1000)
    if not query:
        return _read_failure_cases(limit=limit)[-limit:]
    q_tokens = _tokens(query)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for row in _read_failure_cases(limit=500):
        score = len(q_tokens & _tokens(_failure_text(row)))
        if score:
            scored.append((score, str(row.get("created_at") or ""), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in scored[: int(limit)]]


def _derive_learning_from_event(row: dict[str, Any]) -> None:
    if row.get("kind") != "review_finalized":
        return
    lesson = _lesson_from_review(row)
    if lesson:
        _append_lesson(lesson)


def rebuild_learning_lessons(*, limit: int = 1000) -> dict[str, Any]:
    rows = _all_events_from_jsonl(limit=limit)
    written = 0
    for row in rows:
        lesson = _lesson_from_review(row) if row.get("kind") == "review_finalized" else None
        if lesson:
            _append_lesson(lesson)
            written += 1
    return {
        "lessons_written": written,
        "unique_lessons": len(_read_lessons(limit=limit)),
        "lessons_path": str(_lessons_path()),
    }


def recall_lessons(query: str, *, limit: int = 4) -> list[dict[str, Any]]:
    query = _safe_text(query, limit=1000)
    if not query:
        return _read_lessons(limit=limit)[-limit:]
    q_tokens = _tokens(query)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for lesson in _read_lessons(limit=500):
        score = len(q_tokens & _tokens(_lesson_text(lesson)))
        if score:
            scored.append((score, str(lesson.get("created_at") or ""), lesson))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [lesson for _, _, lesson in scored[: int(limit)]]


def _summarize_lesson(lesson: dict[str, Any]) -> str:
    task_id = lesson.get("root_task_id") or "unknown"
    terms = ", ".join(str(v) for v in (lesson.get("trigger_terms") or [])[:5])
    text = _safe_text(lesson.get("approved_final_text"), limit=120)
    rule = _safe_text(lesson.get("learning_rule"), limit=180)
    return (
        f"{lesson.get('created_at') or ''} lesson {task_id} terms=[{terms}]: "
        f"{text} rule={rule}"
    )


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
    lesson_limit: int = 4,
) -> dict[str, Any]:
    relevant = recall_relevant_events(original_user_text, limit=relevant_limit)
    lessons = recall_lessons(original_user_text, limit=lesson_limit)
    failures = recall_failure_cases(original_user_text, limit=min(3, lesson_limit))
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
        "learned_patterns": [
            _summarize_lesson(lesson) for lesson in lessons
        ],
        "failure_patterns": [
            _summarize_failure_case(row) for row in failures
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
    learned = pack.get("learned_patterns") or []
    failures = pack.get("failure_patterns") or []
    if relevant:
        lines.append("- relevant_spine_events:")
        for item in relevant:
            lines.append(f"  - {_safe_text(item, limit=260)}")
    if learned:
        lines.append("- learned_patterns:")
        for item in learned:
            lines.append(f"  - {_safe_text(item, limit=300)}")
    if failures:
        lines.append("- failure_patterns_to_avoid:")
        for item in failures:
            lines.append(f"  - {_safe_text(item, limit=300)}")
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


def export_graph_jsonl(
    query: str = "",
    *,
    limit: int = 50,
) -> str:
    """Return Graphiti/Zep-ready edge documents as JSONL.

    The export is intentionally a projection of the local spine, not a second
    source of truth. Passing a query exports edges from matching spine events;
    omitting it exports the most recent mirrored edges.
    """
    limit = max(1, int(limit))
    query = _safe_text(query, limit=500)
    if query:
        edges: list[dict[str, Any]] = []
        for row in recall_relevant_events(query, limit=limit):
            edges.extend(_project_graph_edges(row))
    else:
        edges = recent_graph_edges(limit=limit)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for edge in edges:
        edge_id = str(edge.get("edge_id") or "")
        if edge_id and edge_id in seen:
            continue
        if edge_id:
            seen.add(edge_id)
        unique.append(edge)
        if len(unique) >= limit:
            break
    if not unique:
        return ""
    return "\n".join(
        json.dumps(edge, ensure_ascii=False, sort_keys=True) for edge in unique
    ) + "\n"


def _edge_line(edge: dict[str, Any]) -> str:
    return (
        f"{edge.get('created_at') or '-'} "
        f"{edge.get('source') or '-'} "
        f"-[{edge.get('relation') or '-'}]-> "
        f"{edge.get('target') or '-'}"
    )


def build_graph_export_report(query: str = "", *, limit: int = 20) -> str:
    query = _safe_text(query, limit=500)
    raw = export_graph_jsonl(query=query, limit=limit)
    edges: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            edge = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(edge, dict):
            edges.append(edge)

    lines = [
        "Yuri graph export",
        f"- query: {query or '(recent)'}",
        f"- spine_root: {_spine_root()}",
        f"- graph_edges_jsonl: {_graph_edges_path()}",
        f"- exported_edges: {len(edges)}",
        "- source_of_truth: events.jsonl remains primary; graph_edges are a projection for Graphiti/Zep experiments.",
    ]
    if edges:
        lines.append("- edges:")
        for edge in edges[:limit]:
            lines.append(f"  - {_edge_line(edge)}")
    else:
        lines.append("- edges: none")
    return "\n".join(lines)


def _graphiti_episode_from_row(
    row: dict[str, Any],
    *,
    group_id: str = "yuri",
    previous_episode_uuid: str = "",
) -> dict[str, Any]:
    payload = _event_payload(row)
    event_id = str(row.get("event_id") or uuid.uuid4().hex)
    kind = str(row.get("kind") or "event")
    task_id = _event_task_id(payload)
    episode_body = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_kind": kind,
        "task_id": task_id,
        "created_at": row.get("created_at"),
        "payload": _jsonable(payload),
        "projected_edges": _project_graph_edges(row),
    }
    arguments: dict[str, Any] = {
        "name": f"Yuri {kind} {event_id[:8]}",
        "episode_body": json.dumps(episode_body, ensure_ascii=False, sort_keys=True),
        "source": "json",
        "source_description": "Hermes Yuri knowledge spine",
        "reference_time": row.get("created_at") or _utc_iso(),
        "group_id": group_id or "yuri",
    }
    if previous_episode_uuid:
        arguments["previous_episode_uuids"] = [previous_episode_uuid]
    return {
        "schema_version": GRAPHITI_EPISODE_SCHEMA_VERSION,
        "event_id": event_id,
        "event_kind": kind,
        "task_id": task_id,
        "created_at": row.get("created_at"),
        "graphiti_tool": "add_memory",
        "graphiti_legacy_tool": "add_episode",
        "arguments": arguments,
    }


def export_graphiti_episodes(
    query: str = "",
    *,
    limit: int = 200,
    output_path: Optional[str | Path] = None,
    group_id: str = "yuri",
) -> dict[str, Any]:
    """Write Graphiti MCP add_memory-ready episode JSONL from Yuri spine events."""
    limit = max(1, int(limit))
    query = _safe_text(query, limit=500)
    rows = recall_relevant_events(query, limit=limit) if query else _all_events_from_jsonl(limit=limit)
    out = Path(output_path).expanduser() if output_path else _graphiti_episodes_path()
    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    previous_episode_uuid = ""
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            episode = _graphiti_episode_from_row(
                row,
                group_id=group_id,
                previous_episode_uuid=previous_episode_uuid,
            )
            f.write(json.dumps(episode, ensure_ascii=False, sort_keys=True) + "\n")
            previous_episode_uuid = str(episode.get("event_id") or "")
            written += 1
    return {
        "schema_version": GRAPHITI_EPISODE_SCHEMA_VERSION,
        "query": query,
        "group_id": group_id or "yuri",
        "output_path": str(out),
        "episodes_exported": written,
        "source_of_truth": str(_events_path()),
        "graph_edges_jsonl": str(_graph_edges_path()),
    }


def build_graphiti_export_report(
    query: str = "",
    *,
    limit: int = 200,
    output_path: Optional[str | Path] = None,
    group_id: str = "yuri",
) -> str:
    result = export_graphiti_episodes(
        query=query,
        limit=limit,
        output_path=output_path,
        group_id=group_id,
    )
    return "\n".join(
        [
            "Yuri Graphiti export",
            f"- query: {result['query'] or '(recent/all)'}",
            f"- schema_version: {result['schema_version']}",
            f"- group_id: {result['group_id']}",
            f"- output_path: {result['output_path']}",
            f"- episodes_exported: {result['episodes_exported']}",
            f"- source_of_truth: {result['source_of_truth']}",
            "- ingest_note: each JSONL line contains Graphiti MCP add_memory arguments; load only after Graphiti/FalkorDB or Neo4j is running.",
        ]
    )


_OKF_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _okf_slug(value: Any, *, fallback: str = "unknown") -> str:
    text = _safe_text(value, limit=120)
    if not text:
        text = fallback
    slug = _OKF_FILE_RE.sub("_", text).strip("._")
    return slug or fallback


def _yaml_scalar(value: Any) -> str:
    text = _safe_text(value, limit=2000)
    return json.dumps(text, ensure_ascii=False)


def _yaml_list(values: list[Any]) -> str:
    return "[" + ", ".join(_yaml_scalar(v) for v in values if str(v or "").strip()) + "]"


def _okf_frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}: {_yaml_list(value)}")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _md_text(value: Any, *, limit: int = 2000) -> str:
    return _safe_text(value, limit=limit).replace("\n", "\n\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _event_okf_doc(row: dict[str, Any]) -> tuple[str, str]:
    payload = _event_payload(row)
    event_id = str(row.get("event_id") or uuid.uuid4().hex)
    kind = str(row.get("kind") or "event")
    task_id = _event_task_id(payload)
    title = f"{kind} {event_id[:8]}"
    description = _summarize_event(row)
    fields = {
        "type": "Yuri Spine Event",
        "title": title,
        "description": description,
        "resource": f"hermes://yuri-spine/events/{event_id}",
        "tags": ["hermes", "yuri", "memory", kind],
        "timestamp": row.get("created_at"),
        "event_id": event_id,
        "event_kind": kind,
        "task_id": task_id,
    }
    body = [
        _okf_frontmatter(fields),
        f"# {title}",
        "",
        "## Summary",
        _md_text(description, limit=1000),
    ]
    if task_id:
        body.extend(["", "## Related Concepts", f"- [Task {task_id}](/tasks/{_okf_slug(task_id)}.md)"])
    if kind == "yuri_intake":
        body.extend(
            [
                "",
                "## User Intent",
                _md_text(payload.get("original_user_text"), limit=2000),
                "",
                "## Operating Rules",
            ]
        )
        for rule in payload.get("operating_rules") or []:
            body.append(f"- {_md_text(rule, limit=400)}")
    elif kind == "review_finalized":
        body.extend(
            [
                "",
                "## Approved Result",
                _md_text(payload.get("approved_final_text"), limit=2400),
                "",
                "## Review Metadata",
                f"- intent_source: `{_safe_text(payload.get('intent_source'), limit=120) or 'unknown'}`",
                f"- reviewer_task_id: `{_safe_text(payload.get('reviewer_task_id'), limit=120) or 'unknown'}`",
                f"- board: `{_safe_text(payload.get('board'), limit=120) or 'unknown'}`",
            ]
        )
    body.extend(
        [
            "",
            "## Raw Payload",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )
    return f"events/{_okf_slug(event_id)}.md", "\n".join(body)


def _task_okf_doc(task_id: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
    latest = rows[-1] if rows else {}
    description = f"Yuri task memory concept with {len(rows)} related spine event(s)."
    fields = {
        "type": "Yuri Task",
        "title": f"Yuri Task {task_id}",
        "description": description,
        "resource": f"hermes://kanban/tasks/{task_id}",
        "tags": ["hermes", "yuri", "task", "memory"],
        "timestamp": latest.get("created_at") or _utc_iso(),
        "task_id": task_id,
    }
    body = [
        _okf_frontmatter(fields),
        f"# Yuri Task {task_id}",
        "",
        description,
        "",
        "## Related Spine Events",
    ]
    for row in rows:
        event_id = str(row.get("event_id") or "")
        body.append(
            f"- [{_summarize_event(row)}](/events/{_okf_slug(event_id)}.md)"
        )
    return f"tasks/{_okf_slug(task_id)}.md", "\n".join(body)


def export_okf_bundle(
    query: str = "",
    *,
    limit: int = 200,
    output_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Write a local OKF v0.1 bundle projected from Yuri knowledge-spine events."""
    limit = max(1, int(limit))
    query = _safe_text(query, limit=500)
    rows = recall_relevant_events(query, limit=limit) if query else _all_events_from_jsonl(limit=limit)
    root = Path(output_dir).expanduser() if output_dir else _okf_bundle_path()
    root.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    task_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rel, doc = _event_okf_doc(row)
        _write_text(root / rel, doc)
        written.append(rel)
        task_id = _event_task_id(_event_payload(row))
        if task_id:
            task_rows.setdefault(task_id, []).append(row)

    for task_id, related in sorted(task_rows.items()):
        rel, doc = _task_okf_doc(task_id, related)
        _write_text(root / rel, doc)
        written.append(rel)

    events_index = ["# Events", ""]
    for row in rows:
        event_id = str(row.get("event_id") or "")
        events_index.append(
            f"* [{_summarize_event(row)}]({_okf_slug(event_id)}.md)"
        )
    if len(events_index) == 2:
        events_index.append("* No event concepts exported.")
    _write_text(root / "events" / "index.md", "\n".join(events_index))

    tasks_index = ["# Tasks", ""]
    for task_id, related in sorted(task_rows.items()):
        tasks_index.append(
            f"* [Yuri Task {task_id}]({_okf_slug(task_id)}.md) - {len(related)} related event(s)"
        )
    if len(tasks_index) == 2:
        tasks_index.append("* No task concepts exported.")
    _write_text(root / "tasks" / "index.md", "\n".join(tasks_index))

    index = [
        _okf_frontmatter({"okf_version": OKF_VERSION}),
        "# Yuri Knowledge Spine OKF Bundle",
        "",
        "This bundle projects Hermes Yuri knowledge-spine memory into OKF v0.1 Markdown concepts.",
        "",
        "# Concepts",
        f"* [Events](events/) - {len(rows)} exported spine event concept(s).",
        f"* [Tasks](tasks/) - {len(task_rows)} exported task concept(s).",
    ]
    _write_text(root / "index.md", "\n".join(index))

    today = _utc_iso()[:10]
    log = [
        "# Bundle Update Log",
        "",
        f"## {today}",
        f"* **Export**: Generated {len(rows)} event concept(s) and {len(task_rows)} task concept(s) from Yuri knowledge spine.",
    ]
    _write_text(root / "log.md", "\n".join(log))

    return {
        "bundle_root": str(root),
        "okf_version": OKF_VERSION,
        "query": query,
        "events_exported": len(rows),
        "tasks_exported": len(task_rows),
        "files_written": len(written) + 4,
        "concept_files_written": len(written),
    }


def build_okf_export_report(
    query: str = "",
    *,
    limit: int = 200,
    output_dir: Optional[str | Path] = None,
) -> str:
    result = export_okf_bundle(query=query, limit=limit, output_dir=output_dir)
    lines = [
        "Yuri OKF export",
        f"- query: {result['query'] or '(recent/all)'}",
        f"- okf_version: {result['okf_version']}",
        f"- bundle_root: {result['bundle_root']}",
        f"- events_exported: {result['events_exported']}",
        f"- tasks_exported: {result['tasks_exported']}",
        f"- concept_files_written: {result['concept_files_written']}",
        "- conformance: non-reserved concept files include YAML frontmatter with type; index.md/log.md reserved files generated.",
    ]
    return "\n".join(lines)


def build_learning_report(query: str = "", *, limit: int = 8) -> str:
    query = _safe_text(query, limit=500)
    lessons = recall_lessons(query, limit=limit) if query else _read_lessons(limit=limit)
    failures = recall_failure_cases(query, limit=min(limit, 8)) if query else _read_failure_cases(limit=min(limit, 8))
    candidates = _read_regression_candidates(limit=min(limit, 8))
    lines = [
        "Yuri learning report",
        f"- query: {query or '(recent lessons)'}",
        f"- lessons_path: {_lessons_path()}",
        f"- failures_path: {_failures_path()}",
        f"- regression_candidates_path: {_regression_candidates_path()}",
        f"- matched_lessons: {len(lessons)}",
        f"- matched_failure_patterns: {len(failures)}",
        f"- regression_candidates: {len(candidates)}",
        "- mechanism: review_pass lessons and failure patterns are injected into future context packs; failure patterns also create regression-test candidates.",
    ]
    if lessons:
        lines.append("- lessons:")
        for lesson in lessons:
            lines.append(f"  - {_summarize_lesson(lesson)}")
            rule = _safe_text(lesson.get("learning_rule"), limit=220)
            if rule:
                lines.append(f"    - rule: {rule}")
    else:
        lines.append("- lessons: none")
    if failures:
        lines.append("- failure_patterns:")
        for row in failures:
            lines.append(f"  - {_summarize_failure_case(row)}")
            forbidden = _safe_text(row.get("forbidden_behavior"), limit=180)
            if forbidden:
                lines.append(f"    - forbidden: {forbidden}")
    else:
        lines.append("- failure_patterns: none")
    if candidates:
        lines.append("- regression_candidates:")
        for row in candidates[-min(len(candidates), 5) :]:
            lines.append(
                "  - "
                f"{row.get('suggested_test_name')}: "
                f"{_safe_text(row.get('prompt'), limit=120)}"
            )
    else:
        lines.append("- regression_candidates: none")
    return "\n".join(lines)


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

    lessons = recall_lessons(query, limit=min(3, limit)) if query else _read_lessons(limit=min(3, limit))
    if lessons:
        lines.append("- learned_patterns:")
        for lesson in lessons:
            lines.append(f"  - {_summarize_lesson(lesson)}")
    else:
        lines.append("- learned_patterns: none")

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
