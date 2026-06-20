"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")

_YURI_MISSING_PROFILE_FALLBACKS = {
    "backend-eng": "ops",
    "collection-lead": "ops",
    "docs-lead": "docslead",
    "factcheck-lead": "researcher",
    "pm": "planner",
}

_YURI_ARTIFACT_DELIVERY_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:파일|문서|보고서|엑셀|수정본|작성본|산출물|pdf|xlsx|docx|pptx)"
    r".{0,24}(?:전송|첨부|업로드|보냈|보냅니다|전달|드렸)"
    r"|(?:전송|첨부|업로드|보냈|보냅니다|전달|드렸)"
    r".{0,24}(?:파일|문서|보고서|엑셀|수정본|작성본|산출물|pdf|xlsx|docx|pptx)"
    r")",
    re.IGNORECASE,
)


def _looks_like_yuri_task(title: str, body: str, created_by: str) -> bool:
    if created_by in {"yuri", "yuri-review-loop"}:
        return True
    text = f"{title}\n{body}"
    return (
        title.startswith("[YURI intake]")
        or "YURI secretary intake" in text
        or "Telegram 원문" in text
        or "Telegram 의도" in text
        or "대표님 원문" in text
        or "대표님 Telegram 의도" in text
        or "대표님’s" in text
        or "대표님의" in text
        or "telethon source" in text
        or "User intent from Telegram" in text
        or "message_id=" in text
        or "Telegram message_id" in text
        or "상위 질문" in text
        or "상위 사용자 질문" in text
    )


def _resolve_yuri_missing_profile_fallback(old: str, title: str, body: str) -> str | None:
    if old == "factcheck-lead":
        return "researcher"
    if old == "collection-lead":
        text = f"{title}\n{body}"
        if re.search(r"기사|뉴스|품질|퀄리티|WARN|팩트|fact|source", text, re.I):
            return "researcher"
        return "ops"
    if old == "pm":
        return "planner"
    if old == "backend-eng":
        return "ops"
    return _YURI_MISSING_PROFILE_FALLBACKS.get(old)


def format_yuri_telegram_report_text(text: str) -> str:
    """Make Yuri's Telegram reports readable even when workers return prose.

    Writer cards sometimes produce a single Korean paragraph with inline
    ``-`` bullets. Telegram then renders the report as a wall of text. This
    formatter is intentionally conservative: short replies stay as natural
    prose, existing line breaks are preserved, inline bullets are expanded only
    when they are real list markers, and long prose is split into a few
    paragraphs instead of one bullet per sentence.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)

    inline_bullet_count = len(re.findall(r"(?<!^)(?<!\n)[ \t]+-[ \t]+", raw))
    has_existing_structure = bool(re.search(r"(?:^|\n)\s*(?:[-*]|\d+[.)])\s+", raw))
    if inline_bullet_count >= 2 or has_existing_structure:
        raw = re.sub(r"(?<!^)(?<!\n)[ \t]+-[ \t]+", "\n- ", raw)

    compact_sentence_count = len(re.findall(r"[.!?](?:\s|$)", raw))
    if "\n" not in raw and inline_bullet_count == 0 and len(raw) <= 320 and compact_sentence_count <= 3:
        return raw

    raw = re.sub(
        r"(?<=[.!?])\s+(?=(?:따라서|결론|다만|현재|이번|플랫폼|MVP|검증|저장|의사결정|접근|원문|커뮤니티|Reddit|LinkedIn|FM코리아)\b)",
        "\n\n",
        raw,
    )

    out: list[str] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        for line in lines:
            if line.startswith("- "):
                out.append(line)
                continue
            if len(line) <= 150:
                out.append(line)
                continue
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]
            if len(sentences) <= 1:
                out.append(line)
                continue
            paragraph: list[str] = []
            paragraph_len = 0
            for sentence in sentences:
                projected_len = paragraph_len + len(sentence) + (1 if paragraph else 0)
                if paragraph and projected_len > 190:
                    out.append(" ".join(paragraph))
                    out.append("")
                    paragraph = [sentence]
                    paragraph_len = len(sentence)
                else:
                    paragraph.append(sentence)
                    paragraph_len = projected_len
            if paragraph:
                out.append(" ".join(paragraph))
        out.append("")

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def repair_yuri_missing_profile_assignees(
    conn: sqlite3.Connection,
    *,
    profile_exists=None,
    assign_task=None,
) -> list[tuple[str, str, str]]:
    """Map Yuri planner pseudo-roles to real worker profiles before dispatch.

    Yuri can ask the planner to fan out work using natural team labels such as
    ``collection-lead``. Those are not Hermes profiles, so the dispatcher
    correctly refuses to spawn them. For Telegram/Yuri work only, repair the
    known pseudo-roles to the closest real profiles so the queue keeps moving.
    """
    if profile_exists is None:
        try:
            from hermes_cli.profiles import profile_exists as _profile_exists
        except Exception:
            _profile_exists = None
        profile_exists = _profile_exists
    if assign_task is None:
        try:
            from hermes_cli import kanban_db as _kb
            assign_task = _kb.assign_task
        except Exception:
            assign_task = None
    if assign_task is None:
        return []

    aliases = tuple(_YURI_MISSING_PROFILE_FALLBACKS.keys())
    placeholders = ",".join("?" for _ in aliases)
    rows = conn.execute(
        "SELECT id, title, body, assignee, created_by FROM tasks "
        "WHERE status IN ('todo', 'ready') "
        "AND claim_lock IS NULL "
        f"AND assignee IN ({placeholders})",
        aliases,
    ).fetchall()

    repaired: list[tuple[str, str, str]] = []
    for row in rows:
        old = (row["assignee"] or "").strip()
        title = row["title"] or ""
        body = row["body"] or ""
        new = _resolve_yuri_missing_profile_fallback(old, title, body)
        if not new:
            continue
        if profile_exists is not None and not profile_exists(new):
            continue
        if not _looks_like_yuri_task(
            title,
            body,
            row["created_by"] or "",
        ):
            continue
        if assign_task(conn, row["id"], new):
            repaired.append((row["id"], old, new))
    return repaired


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    @staticmethod
    def _yuri_work_review_human_age(seconds: int) -> str:
        seconds = max(0, int(seconds or 0))
        if seconds < 60:
            return "방금"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}분 전"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}시간 전"
        return f"{hours // 24}일 전"

    @staticmethod
    def _yuri_work_review_detail(payload_text: str | None, fallback: str | None = None) -> str:
        detail = ""
        if payload_text:
            try:
                payload = json.loads(payload_text)
                if isinstance(payload, dict):
                    for key in ("reason", "summary", "error", "message"):
                        value = payload.get(key)
                        if value:
                            detail = str(value)
                            break
            except Exception:
                detail = str(payload_text)
        if not detail and fallback:
            detail = str(fallback)
        detail = re.sub(r"\s+", " ", detail).strip()
        if len(detail) > 90:
            detail = detail[:87].rstrip() + "..."
        return detail

    def _build_yuri_work_review_snapshot(self) -> Optional[tuple[str, str]]:
        """Return a stable fingerprint and Telegram text for Yuri work review.

        The hourly review is meant to ask about real open office work, not to
        repeat every old Kanban artifact. Scope it to Yuri/Telegram-visible
        work and suppress duplicate snapshots with a compact fingerprint.
        """
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.debug("Yuri work review: kanban_db unavailable", exc_info=True)
            return None

        try:
            boards = _kb.list_boards(include_archived=False)
        except Exception:
            try:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            except Exception:
                boards = [{"slug": getattr(_kb, "DEFAULT_BOARD", "default")}]

        now = int(time.time())
        seen_db_paths: set[str] = set()
        items: list[dict[str, Any]] = []
        for board_meta in boards:
            slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
            db_path = board_meta.get("db_path")
            try:
                resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
            except Exception:
                resolved_db_path = f"slug:{slug}"
            if resolved_db_path in seen_db_paths:
                continue
            seen_db_paths.add(resolved_db_path)
            try:
                conn = _kb.connect(board=slug)
            except Exception:
                logger.debug("Yuri work review: cannot open board %s", slug, exc_info=True)
                continue
            try:
                rows = conn.execute(
                    """
                    SELECT t.*
                      FROM tasks t
                     WHERE t.status IN ('running', 'ready', 'todo', 'review', 'blocked')
                       AND (
                           t.created_by LIKE 'yuri%'
                           OR t.title LIKE '[YURI intake]%'
                           OR EXISTS (
                               SELECT 1
                                 FROM kanban_notify_subs ns
                                WHERE ns.task_id = t.id
                                  AND lower(ns.platform) = 'telegram'
                           )
                       )
                     ORDER BY
                       CASE t.status
                         WHEN 'blocked' THEN 0
                         WHEN 'running' THEN 1
                         WHEN 'review' THEN 2
                         WHEN 'ready' THEN 3
                         ELSE 4
                       END,
                       COALESCE(t.last_heartbeat_at, t.started_at, t.created_at) DESC,
                       t.priority DESC
                     LIMIT 30
                    """
                ).fetchall()
                for row in rows:
                    event_row = conn.execute(
                        """
                        SELECT kind, payload, created_at
                          FROM task_events
                         WHERE task_id = ?
                           AND kind IN ('blocked', 'gave_up', 'crashed', 'timed_out', 'completed')
                         ORDER BY id DESC
                         LIMIT 1
                        """,
                        (row["id"],),
                    ).fetchone()
                    started = row["started_at"] or row["created_at"] or now
                    last_seen = row["last_heartbeat_at"] or row["started_at"] or row["created_at"] or now
                    items.append(
                        {
                            "board": slug,
                            "id": row["id"],
                            "title": re.sub(r"^\[YURI intake\]\s*", "", row["title"] or "").strip(),
                            "status": row["status"],
                            "assignee": row["assignee"] or "",
                            "age": max(0, now - int(started)),
                            "idle": max(0, now - int(last_seen)),
                            "detail": self._yuri_work_review_detail(
                                event_row["payload"] if event_row else None,
                                row["last_failure_error"],
                            ),
                        }
                    )
            finally:
                conn.close()

        if not items:
            return None

        groups = {
            "blocked": [item for item in items if item["status"] == "blocked"],
            "running": [item for item in items if item["status"] == "running"],
            "review": [item for item in items if item["status"] == "review"],
            "waiting": [item for item in items if item["status"] in {"ready", "todo"}],
        }
        fingerprint = json.dumps(
            [
                (item["board"], item["id"], item["status"], item["assignee"], item["detail"])
                for item in items
            ],
            ensure_ascii=False,
            sort_keys=True,
        )

        def _line(item: dict[str, Any]) -> str:
            title = item["title"] or item["id"]
            if len(title) > 54:
                title = title[:51].rstrip() + "..."
            suffix = ""
            if item["status"] == "blocked" and item["detail"]:
                suffix = f" - {item['detail']}"
            elif item["status"] == "running":
                suffix = f" - {self._yuri_work_review_human_age(item['idle'])} 확인"
            return f"- {title}{suffix}"

        lines = ["현재 진행/보류 업무 점검입니다."]
        if groups["running"]:
            lines.append("")
            lines.append("진행 중")
            lines.extend(_line(item) for item in groups["running"][:4])
        if groups["blocked"]:
            lines.append("")
            lines.append("보류/판단 필요")
            lines.extend(_line(item) for item in groups["blocked"][:5])
        if groups["review"]:
            lines.append("")
            lines.append("검수 대기")
            lines.extend(_line(item) for item in groups["review"][:3])
        if groups["waiting"]:
            lines.append("")
            lines.append("실행 대기")
            lines.extend(_line(item) for item in groups["waiting"][:3])

        extra = len(items) - sum(
            min(len(groups[key]), limit)
            for key, limit in {
                "running": 4,
                "blocked": 5,
                "review": 3,
                "waiting": 3,
            }.items()
        )
        if extra > 0:
            lines.append(f"\n외 {extra}건이 더 있습니다.")
        lines.append("")
        lines.append("상태 점검만 기록했습니다. 우선순위 변경이 필요하면 별도로 알려주세요.")
        return fingerprint, "\n".join(lines)

    @staticmethod
    def _yuri_work_review_status_path() -> Path:
        try:
            from hermes_constants import get_hermes_home

            return get_hermes_home() / "state" / "yuri_work_review_status.json"
        except Exception:
            return Path.home() / ".hermes" / "state" / "yuri_work_review_status.json"

    def _write_yuri_work_review_status(self, **fields: Any) -> None:
        path = self._yuri_work_review_status_path()
        status: dict[str, Any] = {}
        if path.is_file():
            try:
                status = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    status = {}
            except Exception:
                status = {}
        status.update(fields)
        status["updated_at"] = int(time.time())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")

    async def _yuri_work_review_watcher(self) -> None:
        enabled = os.environ.get("HERMES_YURI_WORK_REVIEW_ENABLED", "false").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            self._write_yuri_work_review_status(enabled=False, reason="disabled_by_env")
            logger.info("Yuri work review watcher disabled by env")
            return
        try:
            interval = max(300.0, float(os.environ.get("HERMES_YURI_WORK_REVIEW_INTERVAL_SECONDS", "3600")))
        except ValueError:
            interval = 3600.0
        try:
            initial_delay = max(0.0, float(os.environ.get("HERMES_YURI_WORK_REVIEW_INITIAL_DELAY_SECONDS", str(min(300.0, interval)))))
        except ValueError:
            initial_delay = min(300.0, interval)

        logger.info(
            "Yuri work review watcher enabled interval=%.0fs initial_delay=%.0fs",
            interval,
            initial_delay,
        )
        self._write_yuri_work_review_status(
            enabled=True,
            interval_seconds=int(interval),
            initial_delay_seconds=int(initial_delay),
            state="initial_delay" if initial_delay else "running",
        )
        if initial_delay:
            await asyncio.sleep(initial_delay)

        from gateway.config import Platform as _Platform

        while self._running:
            try:
                adapter = self.adapters.get(_Platform.TELEGRAM)
                home = self.config.get_home_channel(_Platform.TELEGRAM) if getattr(self, "config", None) else None
                self._write_yuri_work_review_status(
                    enabled=True,
                    state="tick",
                    last_tick_at=int(time.time()),
                    telegram_adapter_connected=adapter is not None,
                    home_chat_configured=bool(home and home.chat_id),
                )
                if adapter is not None and home and home.chat_id:
                    snapshot = await asyncio.to_thread(self._build_yuri_work_review_snapshot)
                    self._write_yuri_work_review_status(
                        enabled=True,
                        state="snapshot_checked",
                        last_snapshot_found=bool(snapshot),
                    )
                    if snapshot:
                        fingerprint, message = snapshot
                        self._write_yuri_work_review_status(
                            enabled=True,
                            last_snapshot_fingerprint=fingerprint,
                            last_snapshot_preview=message[:300],
                        )
                        if fingerprint != getattr(self, "_yuri_work_review_last_fingerprint", None):
                            metadata = self._thread_metadata_for_target(
                                _Platform.TELEGRAM,
                                home.chat_id,
                                getattr(home, "thread_id", None),
                                adapter=adapter,
                            )
                            result = await adapter.send(str(home.chat_id), message, metadata=metadata or None)
                            if result is None or getattr(result, "success", True):
                                self._yuri_work_review_last_fingerprint = fingerprint
                                self._yuri_work_review_last_sent_at = time.time()
                                self._write_yuri_work_review_status(
                                    enabled=True,
                                    state="sent",
                                    last_sent_at=int(self._yuri_work_review_last_sent_at),
                                    last_error="",
                                )
                            else:
                                self._write_yuri_work_review_status(
                                    enabled=True,
                                    state="send_failed",
                                    last_error=str(getattr(result, "error", "send returned success=False")),
                                )
                        else:
                            self._write_yuri_work_review_status(
                                enabled=True,
                                state="duplicate_suppressed",
                            )
                else:
                    self._write_yuri_work_review_status(
                        enabled=True,
                        state="waiting_for_telegram_home",
                    )
            except Exception:
                self._write_yuri_work_review_status(
                    enabled=True,
                    state="tick_failed",
                    last_error="tick failed; see gateway.log",
                )
                logger.warning("Yuri work review watcher tick failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the terminal set (``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. When a task reaches a terminal state
        (``completed`` / ``archived``), the subscription is removed.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Subscriptions live inside each board's own DB and cannot
        cross boards, so delivery semantics are unchanged — this is
        purely a fan-out of the single-DB poll.
        """
        # Gate: only the dispatch-owning gateway opens kanban DBs for notifier polling.
        # Non-dispatch gateways have no subscriptions to deliver — all kanban state lives
        # in the dispatch owner's per-board DBs. This prevents N-gateway -shm contention.
        # TODO: gate per-board when per-board dispatcher_owner tracking lands.
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban notifier: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban notifier: disabled via config kanban.dispatch_in_gateway=false"
            )
            return
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")
        # Subscriptions are removed only when the task reaches a truly final
        # status (done / archived). We used to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is genuinely done lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile
        retry_notify_mode = os.environ.get("HERMES_KANBAN_RETRY_NOTIFY_MODE", "first_only").strip().lower()

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                def _collect():
                    deliveries: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(conn)
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                owner_profile = sub.get("notifier_profile") or None
                                if owner_profile and owner_profile != notifier_profile:
                                    logger.debug(
                                        "kanban notifier: subscription for %s owned by profile %s; current profile %s skipping",
                                        sub.get("task_id"), owner_profile, notifier_profile,
                                    )
                                    continue
                                platform = (sub.get("platform") or "").lower()
                                if platform not in active_platforms:
                                    logger.debug(
                                        "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                        sub.get("task_id"), platform or "<missing>",
                                    )
                                    continue
                                old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                    conn,
                                    task_id=sub["task_id"],
                                    platform=sub["platform"],
                                    chat_id=sub["chat_id"],
                                    thread_id=sub.get("thread_id") or "",
                                    kinds=TERMINAL_KINDS,
                                )
                                if not events:
                                    continue
                                task = _kb.get_task(conn, sub["task_id"])
                                latest_run_summary = None
                                latest_run_metadata = None
                                try:
                                    runs = _kb.list_runs(conn, sub["task_id"])
                                    for run in reversed(runs):
                                        if getattr(run, "summary", None) or getattr(run, "metadata", None):
                                            latest_run_summary = getattr(run, "summary", None)
                                            latest_run_metadata = getattr(run, "metadata", None)
                                            break
                                except Exception:
                                    pass
                                logger.debug(
                                    "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                    len(events), sub["task_id"], slug, old_cursor, cursor,
                                )
                                deliveries.append({
                                    "sub": sub,
                                    "old_cursor": old_cursor,
                                    "cursor": cursor,
                                    "events": events,
                                    "task": task,
                                    "board": slug,
                                    "latest_run_summary": latest_run_summary,
                                    "latest_run_metadata": latest_run_metadata,
                                })
                        finally:
                            conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    adapter = self.adapters.get(plat)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    for ev in d["events"]:
                        kind = ev.kind
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        deliver_artifacts = True
                        retry_kind = kind in {"gave_up", "crashed", "timed_out"}
                        if retry_kind and retry_notify_mode == "first_only" and int(d.get("old_cursor") or 0) > 0:
                            logger.info(
                                "kanban notifier: suppressing repeated retry event %s for %s on board %s",
                                kind,
                                sub["task_id"],
                                board_slug,
                            )
                            continue
                        if kind == "completed":
                            if platform_str == "telegram" and await asyncio.to_thread(
                                self._kanban_handoff_sub_to_open_child,
                                sub,
                                task,
                                getattr(ev, "payload", None),
                                board_slug,
                            ):
                                logger.info(
                                    "kanban notifier: handed off subscription from %s to open child on board %s",
                                    sub["task_id"],
                                    board_slug,
                                )
                                continue
                            approved = self._yuri_review_pass_from_metadata(d.get("latest_run_metadata"))
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\n{h}"
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                            handoff_text = handoff.strip()
                            task_instruction_text = "\n".join(
                                part
                                for part in [
                                    str(getattr(task, "title", "") or ""),
                                    str(getattr(task, "body", "") or ""),
                                ]
                                if part
                            )
                            result_text = "\n".join(
                                part
                                for part in [
                                    payload_summary or "",
                                    str(d.get("latest_run_summary") or ""),
                                    str(approved.get("approved_final_text", "") if approved else ""),
                                    str(getattr(task, "result", "") or "") if task else "",
                                ]
                                if part
                            )
                            if platform_str == "telegram":
                                mismatch_msg = self._yuri_handoff_mismatch_warning(
                                    task_instruction_text,
                                    result_text or handoff_text,
                                )
                                if mismatch_msg:
                                    if await asyncio.to_thread(
                                        self._yuri_continue_review_cycle_on_hold,
                                        sub,
                                        task,
                                        getattr(ev, "payload", None),
                                        result_text or handoff_text,
                                        mismatch_msg,
                                        board_slug,
                                    ):
                                        logger.warning(
                                            "Yuri completion guard held %s and started review cycle: %s",
                                            sub["task_id"], mismatch_msg,
                                        )
                                        continue
                                    msg = mismatch_msg
                                    deliver_artifacts = False
                                    try:
                                        self._record_yuri_failure_case(
                                            user_text=task_instruction_text,
                                            observed_behavior=mismatch_msg,
                                            source="completion_guard_hold",
                                            issue_code="misunderstood_intent",
                                            task_id=str(sub["task_id"]),
                                        )
                                    except Exception:
                                        pass
                                    logger.warning(
                                        "Yuri completion guard held %s: %s",
                                        sub["task_id"], mismatch_msg,
                                    )
                                else:
                                    try:
                                        artifact_status = self._kanban_artifact_status(
                                            adapter=adapter,
                                            event_payload=getattr(ev, "payload", None),
                                            task=task,
                                            board_slug=board_slug,
                                        )
                                    except Exception:
                                        artifact_status = None
                                    review_required = self._yuri_review_gate_required(
                                        task_instruction_text,
                                        result_text or handoff_text,
                                        artifact_status=artifact_status,
                                    )
                                    review_metadata = (
                                        d.get("latest_run_metadata")
                                        if isinstance(d.get("latest_run_metadata"), dict)
                                        else getattr(ev, "payload", None)
                                    )
                                    missing_final_review_pass = self._yuri_review_pass_missing_final_text(
                                        review_metadata
                                    )
                                    if missing_final_review_pass:
                                        hold_msg = (
                                            "검수 통과 표시는 있지만 승인된 최종 한글 문안이 없습니다. "
                                            "approved_final_text/final_text가 있는 최종 검수까지 사용자 전송을 보류합니다."
                                        )
                                        if await asyncio.to_thread(
                                            self._yuri_continue_review_cycle_on_hold,
                                            sub,
                                            task,
                                            getattr(ev, "payload", None),
                                            result_text or handoff_text,
                                            hold_msg,
                                            board_slug,
                                        ):
                                            logger.warning(
                                                "Yuri review gate held %s because approved final text is missing",
                                                sub["task_id"],
                                            )
                                            continue
                                        msg = hold_msg
                                        deliver_artifacts = False
                                        try:
                                            self._record_yuri_failure_case(
                                                user_text=task_instruction_text,
                                                observed_behavior=hold_msg,
                                                source="review_gate_hold",
                                                issue_code="completion_without_review",
                                                task_id=str(sub["task_id"]),
                                            )
                                        except Exception:
                                            pass
                                        logger.warning(
                                            "Yuri review gate suppressed incomplete reviewer pass for %s",
                                            sub["task_id"],
                                        )
                                        continue
                                    review_passed = self._yuri_review_gate_passed(
                                        task_instruction_text,
                                        payload_summary or "",
                                        str(d.get("latest_run_summary") or ""),
                                        approved.get("approved_final_text", "") if approved else "",
                                        str(getattr(task, "result", "") or "") if task else "",
                                        handoff_text,
                                        event_payload=review_metadata,
                                    )
                                    data_trust_issue = (
                                        self._yuri_data_trust_gate_issue(
                                            task_instruction_text,
                                            result_text or handoff_text,
                                            event_payload=review_metadata,
                                            artifact_status=artifact_status,
                                        )
                                        if review_passed
                                        else None
                                    )
                                    if review_required and (not review_passed or data_trust_issue):
                                        hold_msg = data_trust_issue or self._yuri_review_gate_hold_message(task_instruction_text)
                                        if await asyncio.to_thread(
                                            self._yuri_continue_review_cycle_on_hold,
                                            sub,
                                            task,
                                            getattr(ev, "payload", None),
                                            result_text or handoff_text,
                                            hold_msg,
                                            board_slug,
                                        ):
                                            logger.warning(
                                                "Yuri review gate held %s and started review cycle",
                                                sub["task_id"],
                                            )
                                            continue
                                        msg = hold_msg
                                        try:
                                            self._record_yuri_failure_case(
                                                user_text=task_instruction_text,
                                                observed_behavior=hold_msg,
                                                source="review_gate_hold",
                                                issue_code=(
                                                    "data_trust_insufficient"
                                                    if data_trust_issue
                                                    else "completion_without_review"
                                                ),
                                                task_id=str(sub["task_id"]),
                                            )
                                        except Exception:
                                            pass
                                        deliver_artifacts = False
                                        logger.warning(
                                            "Yuri review gate held completion for %s",
                                            sub["task_id"],
                                        )
                                    else:
                                        final_text = approved.get("approved_final_text", "") if approved else ""
                                        clean = self._secretary_clean_kanban_text(
                                            platform_str,
                                            final_text or handoff_text or result_text or title,
                                            limit=max(len(final_text) + 32, 260) if final_text else 260,
                                        )
                                        if platform_str == "telegram":
                                            clean = format_yuri_telegram_report_text(clean)
                                        msg = clean or "처리 결과를 확인했습니다."
                            else:
                                msg = (
                                    f"✔ {tag}Kanban {sub['task_id']} done"
                                    f" — {title}{handoff}"
                                )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason = f": {str(ev.payload['reason'])[:160]}"
                            if platform_str == "telegram" and self._yuri_is_review_required_block(task, ev):
                                finalized = await asyncio.to_thread(
                                    self._yuri_finalize_review_blocked_tasks_for_board,
                                    board_slug,
                                    sub["task_id"],
                                )
                                if finalized:
                                    logger.info(
                                        "Yuri review loop finalized %s from reviewer pass",
                                        sub["task_id"],
                                    )
                                    continue
                                msg = "검수 중입니다. 의도 일치와 결과 검증이 끝나면 최종 보고드리겠습니다."
                            elif platform_str == "telegram" and self._kanban_block_is_review_required(ev):
                                handed_off = await asyncio.to_thread(
                                    self._kanban_handoff_blocked_review_required,
                                    sub,
                                    task,
                                    board_slug,
                                )
                                if handed_off:
                                    logger.info(
                                        "kanban notifier: handed off/finalized review-required block %s on board %s",
                                        sub["task_id"],
                                        board_slug,
                                    )
                                    continue
                                msg = "검수 중입니다. 의도 일치와 결과 검증이 끝나면 최종 보고드리겠습니다."
                            elif platform_str == "telegram":
                                detail = reason or ""
                                msg = (
                                    f"작업을 잠시 보류했습니다{detail}. "
                                    "필요한 검수나 추가 확인을 정리한 뒤 다시 보고드리겠습니다."
                                )
                            else:
                                msg = f"⏸ {tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            if platform_str == "telegram":
                                msg = "작업이 반복해서 실패해 아직 완료하지 못했습니다. 필요한 조치를 정리한 뒤 결과만 보고드리겠습니다."
                            else:
                                err = ""
                                if ev.payload and ev.payload.get("error"):
                                    err = f"\n{str(ev.payload['error'])[:200]}"
                                msg = (
                                    f"✖ {tag}Kanban {sub['task_id']} gave up "
                                    f"after repeated spawn failures{err}"
                                )
                        elif kind == "crashed":
                            if platform_str == "telegram":
                                msg = "작업이 중간에 멈췄습니다. 복구 과정은 내부에서 처리하고, 결과가 준비되면 보고드리겠습니다."
                            else:
                                msg = (
                                    f"✖ {tag}Kanban {sub['task_id']} worker crashed "
                                    f"(pid gone); dispatcher will retry"
                                )
                        elif kind == "timed_out":
                            if platform_str == "telegram":
                                msg = "처리 시간이 길어져 중간 점검이 필요합니다. 내부에서 정리한 뒤 결과가 준비되면 보고드리겠습니다."
                            else:
                                limit = 0
                                if ev.payload and ev.payload.get("limit_seconds"):
                                    limit = int(ev.payload["limit_seconds"])
                                msg = (
                                    f"⏱ {tag}Kanban {sub['task_id']} timed out "
                                    f"(max_runtime={limit}s); will retry"
                                )
                        else:
                            continue
                        metadata: dict[str, Any] = {}
                        if sub.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        reply_to = sub.get("reply_to_message_id") or None
                        if reply_to:
                            metadata["telegram_reply_to_message_id"] = reply_to
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            artifact_pre_delivered = False
                            if (
                                kind == "completed"
                                and deliver_artifacts
                                and platform_str == "telegram"
                                and self._yuri_completion_claims_file_delivery(
                                    msg,
                                    result_text if "result_text" in locals() else "",
                                    payload_summary if "payload_summary" in locals() else "",
                                )
                            ):
                                delivered_count = await self._deliver_kanban_artifacts(
                                    adapter=adapter,
                                    chat_id=sub["chat_id"],
                                    metadata=metadata,
                                    reply_to=reply_to,
                                    event_payload=getattr(ev, "payload", None),
                                    task=task,
                                    board_slug=board_slug,
                                )
                                if delivered_count <= 0:
                                    hold_msg = (
                                        "최종 보고 전 첨부 검수를 보강하고 있습니다. "
                                        "대화창에는 검수 통과 후 결과와 파일만 보고드리겠습니다."
                                    )
                                    if await asyncio.to_thread(
                                        self._yuri_continue_review_cycle_on_hold,
                                        sub,
                                        task,
                                        getattr(ev, "payload", None),
                                        result_text if "result_text" in locals() else msg,
                                        hold_msg,
                                        board_slug,
                                    ):
                                        logger.warning(
                                            "Yuri artifact guard held %s and started review cycle",
                                            sub["task_id"],
                                        )
                                        continue
                                    msg = hold_msg
                                    deliver_artifacts = False
                                else:
                                    artifact_pre_delivered = True
                            chunks = self._kanban_split_user_message(msg) if platform_str == "telegram" else [msg]
                            for index, chunk in enumerate(chunks):
                                send_result = await adapter.send(
                                    sub["chat_id"],
                                    chunk,
                                    reply_to=reply_to if index == 0 else None,
                                    metadata=metadata,
                                )
                                if (
                                    send_result is not None
                                    and getattr(send_result, "success", True) is False
                                ):
                                    error = getattr(send_result, "error", None) or "send_result_unsuccessful"
                                    raise RuntimeError(str(error))
                            logger.info(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s (%d chunk%s)",
                                kind,
                                sub["task_id"],
                                platform_str,
                                sub["chat_id"],
                                board_slug,
                                len(chunks),
                                "" if len(chunks) == 1 else "s",
                            )
                            # Persist delivered Kanban completions into the
                            # originating gateway transcript so follow-up
                            # questions in the same chat can see the answer.
                            if kind == "completed" and task and getattr(task, "session_id", None):
                                store = getattr(self, "session_store", None)
                                append = getattr(store, "append_to_transcript", None)
                                if callable(append):
                                    try:
                                        append(
                                            task.session_id,
                                            {"role": "assistant", "content": msg},
                                            skip_db=False,
                                        )
                                    except Exception as transcript_exc:
                                        logger.debug(
                                            "kanban notifier: transcript append failed for %s: %s",
                                            sub["task_id"],
                                            transcript_exc,
                                        )
                            if (
                                kind == "completed"
                                and platform_str == "telegram"
                                and "approved" in locals()
                                and approved
                            ):
                                await asyncio.to_thread(
                                    self._yuri_record_review_delivery_learning,
                                    task,
                                    approved,
                                    board_slug,
                                )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if kind == "completed" and deliver_artifacts and not artifact_pre_delivered:
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        reply_to=reply_to,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                        board_slug=board_slug,
                                    )
                                except Exception as art_exc:
                                    logger.warning(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All events delivered; advance cursor. The cursor
                        # is the dedup mechanism — it prevents re-delivery
                        # of the same event on subsequent ticks.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        # Unsubscribe only when the task has reached a truly
                        # final status (done / archived). For blocked /
                        # gave_up / crashed / timed_out the subscription is
                        # kept alive so the user gets notified again if the
                        # dispatcher respawns the task and it cycles into the
                        # same state. See the longer comment on TERMINAL_KINDS
                        # above for the failure mode this prevents.
                        task_terminal = task and task.status in {"done", "archived"}
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    @staticmethod
    def _kanban_split_user_message(text: str, limit: int = 3900) -> list[str]:
        raw = str(text or "")
        if len(raw) <= limit:
            return [raw]
        chunks: list[str] = []
        remaining = raw
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            cut = remaining.rfind("\n", 0, limit)
            if cut < int(limit * 0.6):
                cut = remaining.rfind(" ", 0, limit)
            if cut < int(limit * 0.6):
                cut = limit
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip()
        return [chunk for chunk in chunks if chunk]

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_handoff_sub_to_open_child(
        self,
        sub: dict,
        task: Any,
        event_payload: Optional[dict[str, Any]],
        board: Optional[str] = None,
    ) -> bool:
        """Move a parent handoff subscription to the child that will report.

        Planner/root cards often complete with "배정 완료" while real work
        continues in child cards. Sending that parent completion as a Yuri
        review hold makes the chat feel broken. Instead, subscribe the same
        Telegram chat to the best open child, preferring reviewer/final-report
        cards, and remove the parent subscription.
        """
        if not task or str(getattr(task, "status", "") or "") != "done":
            return False
        summary = ""
        if isinstance(event_payload, dict):
            summary = str(event_payload.get("summary") or "")
        has_verified_cards = bool(
            isinstance(event_payload, dict)
            and (event_payload.get("verified_cards") or event_payload.get("created_cards"))
        )
        task_text = "\n".join(
            str(part or "")
            for part in (
                getattr(task, "title", ""),
                getattr(task, "body", ""),
                summary,
            )
        )
        if not has_verified_cards and not re.search(r"(?:배정|라우팅|분해|연결|handoff)", task_text, re.IGNORECASE):
            return False

        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        try:
            candidate_ids: list[str] = []
            seen_ids: set[str] = set()

            def _append_candidate(raw: Any) -> None:
                cid = str(raw or "").strip()
                if cid and cid not in seen_ids and cid != str(sub["task_id"]):
                    seen_ids.add(cid)
                    candidate_ids.append(cid)

            if isinstance(event_payload, dict):
                for raw in event_payload.get("verified_cards") or []:
                    _append_candidate(raw)
            for cid in _kb.child_ids(conn, str(sub["task_id"])):
                _append_candidate(cid)
            if not candidate_ids:
                return False

            # Parent cards sometimes only link their immediate worker lanes,
            # while the actual user-facing report is a grandchild synthesis
            # card. Carry the subscription to that final leaf so "I'll report
            # back" does not end at a mid-flow routing message.
            cursor = 0
            while cursor < len(candidate_ids) and len(candidate_ids) < 100:
                cid = candidate_ids[cursor]
                cursor += 1
                try:
                    for child_id in _kb.child_ids(conn, cid):
                        _append_candidate(child_id)
                except Exception:
                    continue

            placeholders = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                f"""
                SELECT id, title, assignee, status
                  FROM tasks
                 WHERE id IN ({placeholders})
                   AND status != 'archived'
                """,
                tuple(candidate_ids),
            ).fetchall()
            if not rows:
                return False
            by_id = {str(row["id"]): row for row in rows}
            ordered = [by_id[cid] for cid in candidate_ids if cid in by_id]

            def _has_open_children(task_id: str) -> bool:
                try:
                    child_ids = _kb.child_ids(conn, task_id)
                except Exception:
                    return False
                if not child_ids:
                    return False
                child_placeholders = ",".join("?" for _ in child_ids)
                found = conn.execute(
                    f"""
                    SELECT 1
                      FROM tasks
                     WHERE id IN ({child_placeholders})
                       AND status NOT IN ('done', 'archived')
                     LIMIT 1
                    """,
                    tuple(child_ids),
                ).fetchone()
                return found is not None

            def _score(row: Any) -> tuple[int, int, int, int, int]:
                text = f"{row['title'] or ''} {row['assignee'] or ''}"
                report_leaf = bool(re.search(
                    r"(?:최종.*(?:보고|검수)|종합\s*보고|보고문|final.*report|synthesis)",
                    text,
                    re.IGNORECASE,
                ))
                finalish = bool(re.search(r"(?:review|reviewer|검수|리뷰|최종|보고문|승인)", text, re.IGNORECASE))
                leaf = not _has_open_children(str(row["id"]))
                open_status = str(row["status"]) not in {"done", "archived"}
                ready = str(row["status"]) == "ready"
                return (
                    1 if report_leaf else 0,
                    1 if leaf else 0,
                    1 if finalish else 0,
                    1 if open_status else 0,
                    1 if ready else 0,
                )

            target = max(ordered, key=_score)
            _kb.add_notify_sub(
                conn,
                task_id=str(target["id"]),
                platform=str(sub["platform"]),
                chat_id=str(sub["chat_id"]),
                thread_id=sub.get("thread_id") or None,
                user_id=sub.get("user_id") or None,
                notifier_profile=sub.get("notifier_profile") or None,
                reply_to_message_id=sub.get("reply_to_message_id") or None,
            )
            _kb.remove_notify_sub(
                conn,
                task_id=str(sub["task_id"]),
                platform=str(sub["platform"]),
                chat_id=str(sub["chat_id"]),
                thread_id=sub.get("thread_id") or "",
            )
            return True
        finally:
            conn.close()

    @staticmethod
    def _yuri_review_fail_from_metadata(raw: Any) -> Optional[dict[str, Any]]:
        if not raw:
            return None
        if isinstance(raw, dict):
            meta = raw
        else:
            try:
                meta = json.loads(str(raw))
            except Exception:
                return None
        status = str(meta.get("review_status", "") or "").strip().lower()
        if status not in {"fail", "failed", "hold", "blocked", "reject", "rejected"}:
            return None
        return dict(meta)

    @staticmethod
    def _yuri_review_cycle_task_text(task: Any, extra: str = "") -> str:
        return "\n".join(
            str(part or "")
            for part in (
                getattr(task, "title", ""),
                getattr(task, "body", ""),
                getattr(task, "result", ""),
                extra,
            )
            if part
        )

    @staticmethod
    def _yuri_completion_claims_file_delivery(*texts: str) -> bool:
        joined = "\n".join(str(text or "") for text in texts)
        return bool(_YURI_ARTIFACT_DELIVERY_CLAIM_RE.search(joined))

    def _kanban_artifact_candidates(
        self,
        *,
        adapter,
        event_payload: Optional[dict],
        task,
        board_slug: Optional[str] = None,
    ) -> tuple[list[str], int]:
        candidates: list[str] = []
        seen: set[str] = set()
        declared_count = 0

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        def _add_from_mapping(mapping: Any) -> None:
            nonlocal declared_count
            if not isinstance(mapping, dict):
                return
            raw = mapping.get("artifacts")
            if isinstance(raw, (list, tuple)):
                declared_count += len([p for p in raw if isinstance(p, str) and p])
                for item in raw:
                    if isinstance(item, str):
                        _add(item)
            for key in (
                "approved_file",
                "approved_manifest",
                "file",
                "output_path",
                "manifest_path",
            ):
                item = mapping.get(key)
                if isinstance(item, str):
                    declared_count += 1
                    _add(item)

        if isinstance(event_payload, dict):
            _add_from_mapping(event_payload)
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary and hasattr(adapter, "extract_local_files"):
                paths, _ = adapter.extract_local_files(summary)
                for path in paths:
                    _add(path)

        if (
            task is not None
            and getattr(task, "result", None)
            and hasattr(adapter, "extract_local_files")
        ):
            paths, _ = adapter.extract_local_files(str(task.result))
            for path in paths:
                _add(path)

        if not candidates and task is not None:
            try:
                from hermes_cli import kanban_db as _kb

                conn = _kb.connect(board=board_slug)
                try:
                    task_id = str(getattr(task, "id", "") or "")
                    try:
                        runs = _kb.list_runs(conn, task_id)
                    except Exception:
                        runs = []
                    for run in reversed(runs):
                        metadata = getattr(run, "metadata", None)
                        if metadata:
                            _add_from_mapping(metadata)
                            if candidates:
                                break
                    for parent_id in _kb.parent_ids(conn, task_id):
                        row = conn.execute(
                            """
                            SELECT payload
                              FROM task_events
                             WHERE task_id = ?
                               AND kind = 'completed'
                               AND payload IS NOT NULL
                             ORDER BY id DESC
                             LIMIT 1
                            """,
                            (parent_id,),
                        ).fetchone()
                        if not row or not row["payload"]:
                            continue
                        try:
                            parent_payload = json.loads(row["payload"])
                        except Exception:
                            continue
                        _add_from_mapping(parent_payload)
                        if not candidates:
                            try:
                                parent_runs = _kb.list_runs(conn, parent_id)
                            except Exception:
                                parent_runs = []
                            for run in reversed(parent_runs):
                                metadata = getattr(run, "metadata", None)
                                if metadata:
                                    _add_from_mapping(metadata)
                                    if candidates:
                                        break
                finally:
                    conn.close()
            except Exception as exc:
                logger.debug(
                    "kanban notifier: parent artifact fallback failed for %s: %s",
                    getattr(task, "id", "?"),
                    exc,
                )

        from gateway.platforms.base import BasePlatformAdapter

        return BasePlatformAdapter.filter_local_delivery_paths(candidates), declared_count

    @staticmethod
    def _telegram_user_visible_artifact_paths(paths: list[str]) -> tuple[list[str], list[str]]:
        """Split Telegram/Yuri artifacts into user-readable uploads and internal evidence.

        Mobile Telegram makes JSON evidence hard to use. Keep structured
        evidence in metadata/logs but do not push it as a representative-facing
        attachment when a readable companion file exists.
        """
        hidden_exts = {".json", ".jsonl", ".ndjson"}
        visible: list[str] = []
        hidden: list[str] = []
        for path in paths:
            if Path(path).suffix.lower() in hidden_exts:
                hidden.append(path)
            else:
                visible.append(path)
        return visible, hidden

    def _kanban_artifact_status(
        self,
        *,
        adapter,
        event_payload: Optional[dict],
        task,
        board_slug: Optional[str] = None,
    ) -> dict[str, Any]:
        candidates, declared_count = self._kanban_artifact_candidates(
            adapter=adapter,
            event_payload=event_payload,
            task=task,
            board_slug=board_slug,
        )
        declared_paths: list[str] = []

        def _collect_declared(mapping: Any) -> None:
            if not isinstance(mapping, dict):
                return
            raw = mapping.get("artifacts")
            if isinstance(raw, (list, tuple)):
                declared_paths.extend(str(p) for p in raw if isinstance(p, str) and p)
            for key in (
                "approved_file",
                "approved_manifest",
                "file",
                "output_path",
                "manifest_path",
            ):
                value = mapping.get(key)
                if isinstance(value, str) and value:
                    declared_paths.append(value)

        _collect_declared(event_payload)
        if task is not None:
            try:
                from hermes_cli import kanban_db as _kb

                conn = _kb.connect(board=board_slug)
                try:
                    task_id = str(getattr(task, "id", "") or "")
                    try:
                        runs = _kb.list_runs(conn, task_id)
                    except Exception:
                        runs = []
                    for run in reversed(runs):
                        _collect_declared(getattr(run, "metadata", None))
                        if declared_paths:
                            break
                    for parent_id in _kb.parent_ids(conn, task_id):
                        row = conn.execute(
                            """
                            SELECT payload
                              FROM task_events
                             WHERE task_id = ?
                               AND kind = 'completed'
                               AND payload IS NOT NULL
                             ORDER BY id DESC
                             LIMIT 1
                            """,
                            (parent_id,),
                        ).fetchone()
                        if row and row["payload"]:
                            try:
                                _collect_declared(json.loads(row["payload"]))
                            except Exception:
                                pass
                        if not declared_paths:
                            try:
                                parent_runs = _kb.list_runs(conn, parent_id)
                            except Exception:
                                parent_runs = []
                            for run in reversed(parent_runs):
                                _collect_declared(getattr(run, "metadata", None))
                                if declared_paths:
                                    break
                finally:
                    conn.close()
            except Exception:
                pass

        base_dirs: list[str] = []
        if task is not None:
            workspace_path = getattr(task, "workspace_path", None)
            if isinstance(workspace_path, str) and workspace_path:
                base_dirs.append(os.path.expanduser(workspace_path))
            try:
                from hermes_cli import kanban_db as _kb

                conn = _kb.connect(board=board_slug)
                try:
                    for parent_id in _kb.parent_ids(conn, str(getattr(task, "id", "") or "")):
                        row = conn.execute(
                            "SELECT workspace_path FROM tasks WHERE id = ?",
                            (parent_id,),
                        ).fetchone()
                        if row and row["workspace_path"]:
                            base_dirs.append(os.path.expanduser(str(row["workspace_path"])))
                finally:
                    conn.close()
            except Exception:
                pass

        def _declared_exists(path: str) -> bool:
            expanded = os.path.expanduser(path)
            if os.path.isabs(expanded):
                return os.path.isfile(expanded)
            for base_dir in base_dirs:
                if base_dir and os.path.isfile(os.path.join(base_dir, expanded)):
                    return True
            return os.path.isfile(expanded)

        unique_declared = list(dict.fromkeys(os.path.expanduser(path) for path in declared_paths))
        existing_declared = [path for path in unique_declared if _declared_exists(path)]
        missing = [path for path in unique_declared if path not in existing_declared]
        return {
            "declared": declared_count,
            "referenced": len(unique_declared),
            "existing": len(candidates),
            "available": len(candidates),
            "missing": missing,
        }

    def _yuri_record_review_delivery_learning(
        self,
        task,
        approved: dict[str, Any],
        board_slug: Optional[str] = None,
    ) -> None:
        if task is None or not approved:
            return
        assignee = str(getattr(task, "assignee", "") or "")
        created_by = str(getattr(task, "created_by", "") or "")
        task_text = "\n".join(
            str(part or "")
            for part in (
                getattr(task, "title", ""),
                getattr(task, "body", ""),
                approved.get("approved_final_text", ""),
                approved.get("intent_source", ""),
            )
            if part
        )
        if created_by not in {"planner", "yuri", "yuri-review-loop"}:
            return
        if assignee != "reviewer" and not _looks_like_yuri_task(
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "body", "") or ""),
            created_by,
        ):
            return
        if not re.search(r"(?:Yuri|YURI|Telegram|Telethon|텔레쏜|검수|review_status)", task_text, re.IGNORECASE):
            return

        root_task_id = str(getattr(task, "id", "") or "")
        reviewer_task_id = root_task_id
        try:
            from hermes_cli import kanban_db as _kb

            conn = _kb.connect(board=board_slug)
            try:
                parent_ids = _kb.parent_ids(conn, root_task_id)
                if parent_ids and assignee == "reviewer":
                    root_task_id = parent_ids[0]
            finally:
                conn.close()
        except Exception:
            pass
        if not root_task_id:
            return
        try:
            from gateway.yuri_knowledge_spine import record_review_result

            record_review_result(
                root_task_id=root_task_id,
                reviewer_task_id=reviewer_task_id,
                approved_final_text=str(approved.get("approved_final_text") or ""),
                intent_source=str(approved.get("intent_source") or "telethon"),
                board=board_slug,
            )
        except Exception as exc:
            logger.debug(
                "Yuri knowledge spine review delivery record failed for %s: %s",
                root_task_id,
                exc,
            )

    def _yuri_continue_review_cycle_on_hold(
        self,
        sub: dict,
        task: Any,
        event_payload: Optional[dict[str, Any]],
        result_text: str,
        hold_reason: str,
        board: Optional[str] = None,
    ) -> bool:
        """Keep Yuri work internal until a reviewer pass exists.

        If a task tries to report without a passing review, move the chat
        subscription to an existing open child. If there is no child, create
        the next review or rework->review pair and subscribe to the final
        reviewer card. Nothing is sent to Telegram until that reviewer passes.
        """
        if task is None:
            return False

        from hermes_cli import kanban_db as _kb

        task_id = str(getattr(task, "id", "") or sub.get("task_id") or "")
        if not task_id:
            return False
        task_text = self._yuri_review_cycle_task_text(task, result_text)
        yuriish = bool(
            re.search(
                r"(?:YURI|Yuri|\[YURI intake\]|review_status|intent_source|텔레쏜|검수)",
                task_text,
                re.IGNORECASE,
            )
        )
        if not yuriish:
            return False

        data_trust_requirements = ""
        try:
            if self._yuri_data_trust_gate_required(
                task_text,
                f"{result_text}\n{hold_reason}",
            ):
                data_trust_requirements = (
                    "\n\nData trust metadata requirements for data/evidence work:\n"
                    "- Reviewer pass is not enough by itself.\n"
                    "- If the result depends on data, files, counts, public sources, or extracted records, "
                    "completion metadata must include data_trust_status=pass plus concrete evidence.\n"
                    "- Include at least source evidence and verification evidence, for example: "
                    "evidence_checked/source_files/source_urls/output_path, and "
                    "verification_commands/readback_commands/counts/sample_rows/sha256_16/remaining_risks.\n"
                    "- If any evidence is missing, complete with review_status=fail or block instead of approving."
                )
        except Exception:
            data_trust_requirements = ""

        conn = _kb.connect(board=board)
        try:
            if self._kanban_move_sub_to_best_open_child(conn, sub, task_id):
                return True

            meta = event_payload if isinstance(event_payload, dict) else {}
            summary = str(meta.get("summary") or result_text or "").strip()
            failed_review = self._yuri_review_fail_from_metadata(meta)
            is_reviewer = bool(
                str(getattr(task, "assignee", "") or "") == "reviewer"
                or re.search(r"(?:review|reviewer|검수|리뷰|승인)", task_text, re.IGNORECASE)
                or failed_review
            )

            if is_reviewer:
                parent_rows = []
                parent_ids = _kb.parent_ids(conn, task_id)
                if parent_ids:
                    placeholders = ",".join("?" for _ in parent_ids)
                    parent_rows = conn.execute(
                        f"SELECT id, title, body, assignee FROM tasks WHERE id IN ({placeholders})",
                        tuple(parent_ids),
                    ).fetchall()
                base = next(
                    (row for row in parent_rows if str(row["assignee"] or "") != "reviewer"),
                    parent_rows[0] if parent_rows else None,
                )
                rework_assignee = str((base["assignee"] if base else "") or "planner")
                base_title = str((base["title"] if base else getattr(task, "title", "")) or task_id)
                rework_id = _kb.create_task(
                    conn,
                    title=f"[rework] {base_title[:80]}",
                    body=(
                        "Yuri review cycle rework.\n"
                        "Do not report to the user directly. Fix the issues below, then the follow-up reviewer card must approve.\n\n"
                        f"Original reviewed task: {base['id'] if base else task_id}\n"
                        f"Failed/held reviewer task: {task_id}\n"
                        f"Hold reason:\n{hold_reason}\n\n"
                        f"Reviewer/hold evidence:\n{summary or result_text}\n\n"
                        "Completion requirements:\n"
                        "- Address every blocking issue.\n"
                        "- Include concise evidence and changed artifact paths if any.\n"
                        "- Do not include user-facing final wording as approved unless the reviewer passes it."
                        f"{data_trust_requirements}"
                    ),
                    assignee=rework_assignee,
                    created_by="yuri-review-loop",
                    parents=[task_id],
                    priority=20,
                    idempotency_key=f"yuri-review-loop:{task_id}:rework",
                    goal_mode=True,
                )
                review_id = _kb.create_task(
                    conn,
                    title=f"[review] 재검수 {base_title[:72]}",
                    body=(
                        "Yuri review cycle follow-up review.\n"
                        "Review the rework result against the original Telegram/Telethon intent.\n\n"
                        f"Failed/held reviewer task: {task_id}\n"
                        f"Rework task: {rework_id}\n"
                        f"Hold reason:\n{hold_reason}\n\n"
                        "If and only if the result is now acceptable, complete with metadata:\n"
                        'review_status=\"pass\", intent_source=\"telethon\" or \"telegram-safe\", '
                        "and approved_final_text/final_text.\n"
                        "If it still fails, complete with review_status=fail plus blocking_issues; "
                        "the loop will create another rework pass."
                        f"{data_trust_requirements}"
                    ),
                    assignee="reviewer",
                    created_by="yuri-review-loop",
                    parents=[rework_id],
                    priority=20,
                    idempotency_key=f"yuri-review-loop:{task_id}:review",
                    goal_mode=True,
                )
                target_id = review_id
            else:
                review_id = _kb.create_task(
                    conn,
                    title=f"[review] {str(getattr(task, 'title', '') or task_id)[:80]}",
                    body=(
                        "Yuri completion review.\n"
                        "The worker produced a result, but user-facing reporting is held until review passes.\n\n"
                        f"Task to review: {task_id}\n"
                        f"Hold reason:\n{hold_reason}\n\n"
                        f"Worker result/handoff:\n{summary or result_text}\n\n"
                        "Check Telegram/Telethon intent, evidence, and final wording. "
                        "If acceptable, complete with review_status=pass, intent_source=telethon "
                        "or telegram-safe, and approved_final_text/final_text. "
                        "If not acceptable, complete with review_status=fail and blocking_issues; "
                        "the loop will create rework."
                        f"{data_trust_requirements}"
                    ),
                    assignee="reviewer",
                    created_by="yuri-review-loop",
                    parents=[task_id],
                    priority=20,
                    idempotency_key=f"yuri-review-loop:{task_id}:initial-review",
                    goal_mode=True,
                )
                target_id = review_id

            _kb.add_notify_sub(
                conn,
                task_id=target_id,
                platform=str(sub["platform"]),
                chat_id=str(sub["chat_id"]),
                thread_id=sub.get("thread_id") or None,
                user_id=sub.get("user_id") or None,
                notifier_profile=sub.get("notifier_profile") or None,
                reply_to_message_id=sub.get("reply_to_message_id") or None,
            )
            _kb.remove_notify_sub(
                conn,
                task_id=task_id,
                platform=str(sub["platform"]),
                chat_id=str(sub["chat_id"]),
                thread_id=sub.get("thread_id") or "",
            )
            try:
                _kb.add_comment(
                    conn,
                    task_id,
                    "yuri-review-loop",
                    f"Report held and routed to {target_id}. Reason: {hold_reason}",
                )
            except Exception:
                pass
            return True
        finally:
            conn.close()

    def _kanban_move_sub_to_best_open_child(self, conn: Any, sub: dict, task_id: str) -> bool:
        from hermes_cli import kanban_db as _kb

        candidate_ids = _kb.child_ids(conn, task_id)
        if not candidate_ids:
            return False
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = conn.execute(
            f"""
            SELECT id, title, assignee, status
              FROM tasks
             WHERE id IN ({placeholders})
               AND status NOT IN ('done', 'archived')
            """,
            tuple(candidate_ids),
        ).fetchall()
        if not rows:
            return False

        def _score(row: Any) -> tuple[int, int]:
            text = f"{row['title'] or ''} {row['assignee'] or ''}"
            finalish = bool(re.search(r"(?:review|reviewer|검수|리뷰|최종|보고문|승인)", text, re.IGNORECASE))
            return (1 if finalish else 0, 1 if str(row["status"]) in {"ready", "review"} else 0)

        target = max(rows, key=_score)
        _kb.add_notify_sub(
            conn,
            task_id=str(target["id"]),
            platform=str(sub["platform"]),
            chat_id=str(sub["chat_id"]),
            thread_id=sub.get("thread_id") or None,
            user_id=sub.get("user_id") or None,
            notifier_profile=sub.get("notifier_profile") or None,
            reply_to_message_id=sub.get("reply_to_message_id") or None,
        )
        _kb.remove_notify_sub(
            conn,
            task_id=task_id,
            platform=str(sub["platform"]),
            chat_id=str(sub["chat_id"]),
            thread_id=sub.get("thread_id") or "",
        )
        return True

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    @staticmethod
    def _yuri_is_review_required_block(task: Any, ev: Any) -> bool:
        """True for Yuri intake blocks that are waiting on reviewer approval."""
        text = "\n".join(
            str(part or "")
            for part in [
                getattr(task, "title", ""),
                getattr(task, "body", ""),
                (getattr(ev, "payload", None) or {}).get("reason", "")
                if isinstance(getattr(ev, "payload", None), dict)
                else "",
            ]
        )
        return bool(
            ("YURI" in text or "Yuri" in text or "[YURI intake]" in text)
            and re.search(r"review-required|검수|review_status|intent_source", text, re.IGNORECASE)
        )

    @staticmethod
    def _kanban_block_is_review_required(ev: Any) -> bool:
        payload = getattr(ev, "payload", None)
        reason = ""
        if isinstance(payload, dict):
            reason = str(payload.get("reason") or "")
        return bool(re.search(r"review-required|검수.*필요|reviewer", reason, re.IGNORECASE))

    def _kanban_handoff_blocked_review_required(
        self,
        sub: dict,
        task: Any,
        board: Optional[str] = None,
    ) -> bool:
        if task is None:
            return False
        from hermes_cli import kanban_db as _kb

        task_id = str(getattr(task, "id", "") or sub.get("task_id") or "")
        if not task_id:
            return False
        conn = _kb.connect(board=board)
        try:
            if self._kanban_move_sub_to_best_open_child(conn, sub, task_id):
                return True
        finally:
            conn.close()

        finalized = self._yuri_finalize_review_blocked_tasks_for_board(
            board=board,
            task_id=task_id,
            allow_non_yuri=True,
        )
        return bool(finalized)

    @staticmethod
    def _yuri_review_pass_from_metadata(raw: Any) -> Optional[dict[str, Any]]:
        if not raw:
            return None
        if isinstance(raw, dict):
            meta = raw
        else:
            try:
                meta = json.loads(str(raw))
            except Exception:
                return None
        status = str(meta.get("review_status", "") or "").strip().lower()
        source = str(meta.get("intent_source", "") or "").strip().lower()
        if status not in {"pass", "passed", "ok"}:
            return None
        if source not in {"telethon", "telegram-safe", "telegram", "conversation"}:
            return None
        final_text = (
            meta.get("approved_final_text")
            or meta.get("final_text")
            or meta.get("approved_text")
            or ""
        )
        final_text = str(final_text or "").strip()
        if not final_text:
            return None
        out = dict(meta)
        out["intent_source"] = source
        out["approved_final_text"] = final_text
        return out

    @staticmethod
    def _yuri_review_pass_missing_final_text(raw: Any) -> bool:
        if not raw:
            return False
        if isinstance(raw, dict):
            meta = raw
        else:
            try:
                meta = json.loads(str(raw))
            except Exception:
                return False
        status = str(meta.get("review_status", "") or "").strip().lower()
        source = str(meta.get("intent_source", "") or "").strip().lower()
        if status not in {"pass", "passed", "ok"}:
            return False
        if source not in {"telethon", "telegram-safe", "telegram", "conversation"}:
            return False
        final_text = (
            meta.get("approved_final_text")
            or meta.get("final_text")
            or meta.get("approved_text")
            or ""
        )
        return not bool(str(final_text or "").strip())

    def _yuri_finalize_review_blocked_tasks_for_board(
        self,
        board: Optional[str] = None,
        task_id: Optional[str] = None,
        allow_non_yuri: bool = False,
    ) -> list[str]:
        """Close Yuri intake roots once a reviewer pass exists.

        Some planner workers create review children but forget to link them
        as dependencies of the original Yuri intake. The reviewer may still
        complete with the required ``review_status=pass`` metadata, while the
        root stays ``blocked`` forever. This repair pass finds those roots and
        turns the approved reviewer text into the user-facing completion event.
        """
        from hermes_cli import kanban_db as _kb

        conn = _kb.connect(board=board)
        finalized: list[str] = []
        try:
            if task_id:
                roots = conn.execute(
                    """
                    SELECT id, title, body, created_at
                      FROM tasks
                     WHERE id = ?
                       AND status = 'blocked'
                    """,
                    (task_id,),
                ).fetchall()
            else:
                roots = conn.execute(
                    """
                    SELECT id, title, body, created_at
                      FROM tasks
                     WHERE status = 'blocked'
                       AND (
                         title LIKE '[YURI intake]%'
                         OR title LIKE 'Yuri intake:%'
                         OR body LIKE '%YURI secretary intake%'
                         OR body LIKE '%Yuri intake contract%'
                       )
                    """
                ).fetchall()

            for root in roots:
                root_id = str(root["id"])
                root_text = f"{root['title'] or ''}\n{root['body'] or ''}"
                if (
                    not allow_non_yuri
                    and "YURI" not in root_text
                    and "Yuri" not in root_text
                ):
                    continue

                needle = f"%{root_id}%"
                reviewers = conn.execute(
                    """
                    SELECT t.id AS task_id, t.title, t.body,
                           r.summary, r.metadata, r.ended_at
                      FROM task_runs r
                      JOIN tasks t ON t.id = r.task_id
                     WHERE t.assignee = 'reviewer'
                       AND r.outcome = 'completed'
                       AND COALESCE(r.ended_at, 0) >= ?
                       AND (
                         t.body LIKE ?
                         OR t.title LIKE ?
                         OR r.summary LIKE ?
                         OR r.metadata LIKE ?
                       )
                     ORDER BY r.ended_at DESC, r.id DESC
                     LIMIT 20
                    """,
                    (
                        int(root["created_at"] or 0),
                        needle,
                        needle,
                        needle,
                        needle,
                    ),
                ).fetchall()

                approved: Optional[dict[str, Any]] = None
                reviewer_task_id = ""
                for reviewer in reviewers:
                    approved = self._yuri_review_pass_from_metadata(
                        reviewer["metadata"]
                    )
                    if approved:
                        reviewer_task_id = str(reviewer["task_id"])
                        break
                if not approved:
                    continue

                final_text = str(approved["approved_final_text"]).strip()
                data_trust_issue = self._yuri_data_trust_gate_issue(
                    root_text,
                    final_text,
                    event_payload=approved,
                )
                if data_trust_issue:
                    try:
                        _kb.add_comment(
                            conn,
                            root_id,
                            "yuri-review-loop",
                            (
                                "Reviewer pass not finalized because data trust "
                                f"evidence is incomplete: {data_trust_issue}"
                            ),
                        )
                    except Exception:
                        pass
                    continue
                metadata = {
                    "review_status": "pass",
                    "intent_source": approved.get("intent_source", "telethon"),
                    "approved_final_text": final_text,
                    "reviewer_task": reviewer_task_id,
                    "auto_finalized_from_review": True,
                }
                artifacts: list[str] = []
                for key in ("approved_file", "approved_manifest", "file", "output_path", "manifest_path"):
                    value = approved.get(key)
                    if isinstance(value, str) and value:
                        metadata[key] = value
                        artifacts.append(value)
                if artifacts:
                    metadata["artifacts"] = artifacts
                if _kb.complete_task(
                    conn,
                    root_id,
                    result=final_text,
                    summary=final_text,
                    metadata=metadata,
                ):
                    try:
                        _kb.add_comment(
                            conn,
                            root_id,
                            "yuri-review-loop",
                            (
                                f"Auto-finalized from reviewer {reviewer_task_id}: "
                                f"review_status=pass, intent_source={metadata['intent_source']}."
                            ),
                        )
                    except Exception:
                        pass
                    try:
                        from gateway.yuri_knowledge_spine import record_review_result

                        record_review_result(
                            root_task_id=root_id,
                            reviewer_task_id=reviewer_task_id,
                            approved_final_text=final_text,
                            intent_source=str(metadata["intent_source"]),
                            board=board,
                        )
                    except Exception as spine_exc:
                        logger.debug(
                            "Yuri knowledge spine review record failed for %s: %s",
                            root_id,
                            spine_exc,
                        )
                    finalized.append(root_id)
        finally:
            conn.close()
        return finalized

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
        reply_to: Optional[str] = None,
        board_slug: Optional[str] = None,
    ) -> int:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)
          4. done parent tasks' completion artifacts (review-card fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        candidates, declared_count = self._kanban_artifact_candidates(
            adapter=adapter,
            event_payload=event_payload,
            task=task,
            board_slug=board_slug,
        )

        if not candidates:
            if declared_count:
                logger.warning(
                    "kanban notifier: %d declared artifact(s) but no existing local files to upload",
                    declared_count,
                )
            return 0
        if str(getattr(adapter, "name", "") or "").lower() == "telegram":
            visible_candidates, hidden_candidates = self._telegram_user_visible_artifact_paths(candidates)
            if hidden_candidates:
                logger.info(
                    "kanban notifier: suppressing %d Telegram JSON artifact(s) from user-facing upload: %s",
                    len(hidden_candidates),
                    ", ".join(Path(p).name for p in hidden_candidates[:5]),
                )
            candidates = visible_candidates
            if not candidates:
                logger.info(
                    "kanban notifier: all %d Telegram artifact(s) are internal evidence; skipping user-facing upload",
                    len(hidden_candidates),
                )
                return 0

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if Path(p).suffix.lower() not in _IMAGE_EXTS]
        delivered_count = 0

        if image_paths:
            try:
                batch = [
                    (f"file://{_quote(p)}", f"첨부: {Path(p).name}")
                    for p in image_paths
                ]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
                delivered_count += len(image_paths)
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = Path(path).suffix.lower()
            try:
                caption = f"첨부: {Path(path).name}"
                if ext in _VIDEO_EXTS:
                    result = await adapter.send_video(
                        chat_id=chat_id,
                        video_path=path,
                        caption=caption,
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                    if result is None or getattr(result, "success", False):
                        delivered_count += 1
                    else:
                        logger.warning(
                            "kanban notifier: video artifact upload returned failure for %s: %s",
                            path,
                            getattr(result, "error", ""),
                        )
                else:
                    result = await adapter.send_document(
                        chat_id=chat_id,
                        file_path=path,
                        caption=caption,
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                    if result is None or getattr(result, "success", False):
                        delivered_count += 1
                    else:
                        logger.warning(
                            "kanban notifier: document artifact upload returned failure for %s: %s",
                            path,
                            getattr(result, "error", ""),
                        )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )
        return delivered_count

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info(f"kanban dispatcher: max_spawn={max_spawn}")

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info(f"kanban dispatcher: max_in_progress={max_in_progress}")

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                self._yuri_finalize_review_blocked_tasks_for_board(slug)
                conn = _kb.connect(board=slug)
                repaired_assignees = repair_yuri_missing_profile_assignees(conn)
                if repaired_assignees:
                    logger.info(
                        "kanban dispatcher [%s]: repaired Yuri pseudo-assignees: %s",
                        slug,
                        ", ".join(
                            f"{tid}:{old}->{new}"
                            for tid, old, new in repaired_assignees
                        ),
                    )
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        auto_decompose_enabled = bool(kanban_cfg.get("auto_decompose", True))
        try:
            auto_decompose_per_tick = int(
                kanban_cfg.get("auto_decompose_per_tick", 3) or 3
            )
        except (TypeError, ValueError):
            auto_decompose_per_tick = 3
        if auto_decompose_per_tick < 1:
            auto_decompose_per_tick = 1

        def _auto_decompose_tick() -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await asyncio.to_thread(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                if auto_decompose_enabled:
                    await asyncio.to_thread(_auto_decompose_tick)
                results = await asyncio.to_thread(_tick_once)
                any_spawned = False
                for slug, res in (results or []):
                    if res is not None and getattr(res, "spawned", None):
                        any_spawned = True
                        # Quiet by default — only log when something actually
                        # happened, so an idle gateway stays silent.
                        logger.info(
                            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                            slug,
                            len(res.spawned),
                            res.reclaimed,
                            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                            res.promoted,
                            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                        )
                # Health telemetry (aggregate across boards)
                ready_pending = await asyncio.to_thread(_ready_nonempty)
                if ready_pending and not any_spawned:
                    bad_ticks += 1
                else:
                    bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0
