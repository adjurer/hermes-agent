import asyncio
import json
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "reply_to": reply_to,
            "metadata": metadata or {},
        })


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


class TranscriptStore:
    def __init__(self):
        self.entries = []

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.entries.append((session_id, message))


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner.session_store = TranscriptStore()
    return runner


def _create_completed_subscription(summary="done once", reply_to_message_id=None):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id=reply_to_message_id,
        )
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "마무리했습니다" in adapter.sent[0]["text"]
    assert "Kanban" not in adapter.sent[0]["text"]
    assert tid not in adapter.sent[0]["text"]


def test_kanban_notifier_replies_to_original_message(tmp_path, monkeypatch):
    db_path = tmp_path / "reply-anchor.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    _create_completed_subscription(reply_to_message_id="462")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["reply_to"] == "462"
    assert adapter.sent[0]["metadata"]["telegram_reply_to_message_id"] == "462"


def test_yuri_notifier_uses_approved_final_text_from_run_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-approved-final.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="[YURI intake] TID=WRK-T01 업무형 검토",
            body=(
                "YURI secretary intake from Telegram.\n"
                "Original user text:\n"
                "TID=WRK-T01 업무형 검토를 진행해주세요."
            ),
            assignee="planner",
            initial_status="running",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        assert kb.complete_task(
            conn,
            tid,
            summary="review_status=pass, intent_source=telethon: worker summary",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": "TID=WRK-T01 승인된 최종 문안입니다.",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"] == "TID=WRK-T01 승인된 최종 문안입니다."


def test_yuri_notifier_does_not_truncate_approved_final_text(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-long-approved-final.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    long_final = "승인된 최종 보고입니다. " + ("상세 검수 내용입니다. " * 650) + "끝표식-LONG-FINAL"

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="[YURI intake] 긴 최종 보고",
            body="YURI secretary intake from Telegram.\nOriginal user text:\n긴 보고를 해주세요.",
            assignee="reviewer",
            initial_status="running",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        assert kb.complete_task(
            conn,
            tid,
            summary="review_status=pass, intent_source=telethon",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": long_final,
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) > 1
    combined = "\n".join(item["text"] for item in adapter.sent)
    assert "끝표식-LONG-FINAL" in combined
    assert len(combined) > 3900
    assert adapter.sent[0]["reply_to"] is None


def test_kanban_notifier_persists_completion_for_followup_context(tmp_path, monkeypatch):
    db_path = tmp_path / "context-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="에르메스 코덱스 기능 조사",
            assignee="researcher",
            session_id="session-direct-chat",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="1. Codex CLI 위임 2. Codex lane")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert runner.session_store.entries
    session_id, message = runner.session_store.entries[-1]
    assert session_id == "session-direct-chat"
    assert message["role"] == "assistant"
    assert "Codex CLI" in message["content"]


def test_kanban_notifier_suppresses_planner_handoff_when_children_open(tmp_path, monkeypatch):
    db_path = tmp_path / "planner-handoff-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn,
            title="주소 찾아서 보내기",
            assignee="planner",
            session_id="session-planner",
        )
        kb.create_task(
            conn,
            title="주소 후보 찾기",
            assignee="ops",
            parents=[parent_id],
        )
        kb.add_notify_sub(conn, task_id=parent_id, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, parent_id, summary="배정 완료: ops에게 주소 후보 찾기를 연결했습니다.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []


def test_kanban_notifier_hands_off_verified_cards_without_links(tmp_path, monkeypatch):
    db_path = tmp_path / "verified-card-handoff-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn,
            title="패치 라우팅",
            assignee="planner",
            session_id="session-planner",
        )
        child_id = kb.create_task(
            conn,
            title="패치 검토",
            assignee="reviewer",
            created_by="planner",
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="777",
        )
        kb.complete_task(
            conn,
            parent_id,
            summary="배정 완료: reviewer에게 패치 검토를 연결했습니다.",
            created_cards=[child_id],
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    conn = kb.connect()
    try:
        parent_subs = kb.list_notify_subs(conn, parent_id)
        child_subs = kb.list_notify_subs(conn, child_id)
    finally:
        conn.close()
    assert parent_subs == []
    assert len(child_subs) == 1
    assert child_subs[0]["reply_to_message_id"] == "777"


def test_yuri_unreviewed_completion_creates_review_cycle_without_user_report(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-unreviewed-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="[YURI intake] 보고서 작성",
            body="YURI secretary intake from Telegram.\nOriginal user text:\n보고서 작성해주세요.",
            assignee="writer",
            initial_status="running",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="901",
        )
        assert kb.complete_task(conn, tid, summary="보고서 초안을 작성했습니다.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT id, title, assignee, status FROM tasks WHERE created_by = 'yuri-review-loop'"
        ).fetchall()
        parent_subs = kb.list_notify_subs(conn, tid)
        review_subs = [
            sub
            for row in rows
            for sub in kb.list_notify_subs(conn, row["id"])
        ]
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["assignee"] == "reviewer"
    assert rows[0]["status"] == "ready"
    assert parent_subs == []
    assert len(review_subs) == 1
    assert review_subs[0]["reply_to_message_id"] == "901"


def test_yuri_failed_review_creates_rework_then_followup_review_without_user_report(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-failed-review-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        work_id = kb.create_task(
            conn,
            title="[YURI intake] 기사 품질 감사",
            body="YURI secretary intake from Telegram.\nOriginal user text:\n기사 품질 검증해주세요.",
            assignee="researcher",
            initial_status="running",
        )
        assert kb.complete_task(conn, work_id, summary="품질 감사 결과 초안")
        review_id = kb.create_task(
            conn,
            title="[review] 기사 품질 감사 검수",
            body="review_status metadata required. Check Telegram/Telethon intent.",
            assignee="reviewer",
            parents=[work_id],
            initial_status="running",
        )
        kb.add_notify_sub(
            conn,
            task_id=review_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="902",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review_status=fail, intent_source=telethon: 근거가 부족합니다.",
            metadata={
                "review_status": "fail",
                "intent_source": "telethon",
                "blocking_issues": ["근거 부족"],
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT id, title, assignee, status FROM tasks WHERE created_by = 'yuri-review-loop' ORDER BY created_at, id"
        ).fetchall()
        old_subs = kb.list_notify_subs(conn, review_id)
        followup_review = [row for row in rows if row["assignee"] == "reviewer"]
        followup_subs = [
            sub
            for row in followup_review
            for sub in kb.list_notify_subs(conn, row["id"])
        ]
    finally:
        conn.close()
    assert len(rows) == 2
    assert any(row["assignee"] == "researcher" and row["status"] == "ready" for row in rows)
    assert len(followup_review) == 1
    assert followup_review[0]["status"] == "todo"
    assert old_subs == []
    assert len(followup_subs) == 1
    assert followup_subs[0]["reply_to_message_id"] == "902"


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_yuri_review_loop_finalizes_blocked_root_from_unlinked_reviewer_pass(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "yuri-review-loop-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))
    kb.init_db()

    conn = kb.connect()
    try:
        root_id = kb.create_task(
            conn,
            title="[YURI intake] 여론조사 인식 개선은 어떻게 되고있나요?",
            body=(
                "YURI secretary intake from Telegram.\n"
                "Original user text:\n"
                "여론조사 인식 개선은 어떻게 되고있나요?"
            ),
            assignee="planner",
            initial_status="running",
        )
        assert kb.block_task(
            conn,
            root_id,
            reason="review-required: wait for reviewer approval",
        )
        reviewer_id = kb.create_task(
            conn,
            title="Review Telegram intent for Yuri delivery",
            body=(
                f"Parent intake {root_id}. "
                "Check Telegram/Telethon intent before final delivery."
            ),
            assignee="reviewer",
        )
        assert kb.complete_task(
            conn,
            reviewer_id,
            summary="review_status=pass; intent_source=telethon",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": "검수 통과 문안입니다.",
            },
        )
    finally:
        conn.close()

    runner = _make_runner(RecordingAdapter())
    assert runner._yuri_finalize_review_blocked_tasks_for_board() == [root_id]

    conn = kb.connect()
    try:
        root = kb.get_task(conn, root_id)
        runs = kb.list_runs(conn, root_id)
    finally:
        conn.close()

    assert root.status == "done"
    assert root.result == "검수 통과 문안입니다."
    completed = [run for run in runs if run.outcome == "completed"]
    assert completed
    assert completed[-1].metadata["review_status"] == "pass"
    assert completed[-1].metadata["intent_source"] == "telethon"
    assert completed[-1].metadata["auto_finalized_from_review"] is True

    events_path = tmp_path / "spine" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["kind"] == "review_finalized"
    assert rows[-1]["payload"]["root_task_id"] == root_id
    assert rows[-1]["payload"]["reviewer_task_id"] == reviewer_id


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, reply_to=None, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "중간에 멈췄습니다" in adapter.sent[0]["text"]

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "중간에 멈췄습니다" in adapter.sent[1]["text"]


def test_notifier_can_suppress_repeated_retry_notifications(tmp_path, monkeypatch):
    """Operators can keep retry recovery alive without flooding chat."""
    db_path = tmp_path / "quiet-retry-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_RETRY_NOTIFY_MODE", "first_only")
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="quiet cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1

    conn = kb.connect()
    try:
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert _unseen_terminal_events(tid) == []
