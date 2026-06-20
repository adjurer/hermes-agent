import asyncio
import json
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.documents = []

    async def send(self, chat_id, text, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "reply_to": reply_to,
            "metadata": metadata or {},
        })

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None):
        self.documents.append({
            "chat_id": chat_id,
            "file_path": file_path,
            "caption": caption,
            "file_name": file_name,
            "reply_to": reply_to,
            "metadata": metadata or {},
        })


class FailedSendResult:
    success = False
    error = "synthetic send failure"


class SendResultFailingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "reply_to": reply_to,
            "metadata": metadata or {},
        })
        return FailedSendResult()


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
    assert adapter.sent[0]["text"] == "done once"
    assert "Kanban" not in adapter.sent[0]["text"]
    assert tid not in adapter.sent[0]["text"]


def test_yuri_work_review_snapshot_summarizes_open_telegram_work(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        running_id = kb.create_task(
            conn,
            title="[YURI intake] 샘플 5개열 채우기",
            assignee="planner",
            created_by="yuri-frontdesk",
            initial_status="running",
        )
        blocked_id = kb.create_task(
            conn,
            title="[YURI intake] 지방의회 상세페이지 보강",
            assignee="ops",
            created_by="yuri-frontdesk",
            initial_status="running",
        )
        assert running_id
        conn.execute(
            """
            UPDATE tasks
               SET status = 'running',
                   started_at = created_at,
                   last_heartbeat_at = created_at
             WHERE id = ?
            """,
            (running_id,),
        )
        conn.commit()
        assert kb.block_task(conn, blocked_id, reason="관리자 확인 필요")
    finally:
        conn.close()

    runner = _make_runner(RecordingAdapter())
    snapshot = runner._build_yuri_work_review_snapshot()

    assert snapshot is not None
    _, text = snapshot
    assert "현재 진행/보류 업무 점검입니다." in text
    assert "진행 중" in text
    assert "샘플 5개열 채우기" in text
    assert "보류/판단 필요" in text
    assert "관리자 확인 필요" in text
    assert "상태 점검만 기록했습니다" in text
    assert "이대로 계속 둘까요" not in text


def test_yuri_work_review_status_file_records_runtime_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _make_runner(RecordingAdapter())

    runner._write_yuri_work_review_status(
        enabled=True,
        interval_seconds=3600,
        state="snapshot_checked",
        last_snapshot_found=False,
    )

    status_path = tmp_path / "state" / "yuri_work_review_status.json"
    assert status_path.is_file()
    data = json.loads(status_path.read_text(encoding="utf-8"))
    assert data["enabled"] is True
    assert data["state"] == "snapshot_checked"
    assert data["interval_seconds"] == 3600
    assert "updated_at" in data


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


def test_kanban_notifier_rewinds_when_send_result_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "failed-send-result.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()
    adapter = SendResultFailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
    finally:
        conn.close()
    assert len(subs) == 1
    assert subs[0]["last_event_id"] == 0


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


def test_yuri_internal_review_pass_without_final_text_is_held_and_artifacts_suppressed(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-internal-review-no-final.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    artifact = tmp_path / "deep_interview_lab_evidence.json"
    artifact.write_text('{"internal": true}', encoding="utf-8")

    conn = kb.connect()
    try:
        root_id = kb.create_task(
            conn,
            title="[YURI intake] 테스트 공간 만들어서 도입 테스트",
            body=(
                "YURI secretary intake from Telegram.\n"
                "Original user text:\n"
                "테스트 공간 만들어서 도입 테스트 해줘."
            ),
            assignee="planner",
            initial_status="running",
        )
        lane_id = kb.create_task(
            conn,
            title="[review] Deep Interview lane review for internal evidence",
            body=(
                f"Deep Interview lane review for root {root_id}.\n"
                "review_status metadata required. Check Telegram/Telethon intent.\n"
                "This is an internal evidence lane, not the final Korean report."
            ),
            assignee="reviewer",
            initial_status="running",
        )
        kb.add_notify_sub(
            conn,
            task_id=lane_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="23486",
        )
        assert kb.complete_task(
            conn,
            lane_id,
            summary="review_status=pass, intent_source=telethon: internal lane handoff only",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "artifacts": [str(artifact)],
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.documents == []
    conn = kb.connect()
    try:
        old_subs = kb.list_notify_subs(conn, lane_id)
        review_loop_rows = conn.execute(
            "SELECT title, assignee, status FROM tasks WHERE created_by = 'yuri-review-loop'"
        ).fetchall()
    finally:
        conn.close()
    assert old_subs == []
    assert any(row["assignee"] == "reviewer" for row in review_loop_rows)



def test_yuri_review_completion_delivers_parent_artifact(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-review-parent-artifact.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    artifact = tmp_path / "final.xlsx"
    artifact.write_bytes(b"xlsx")

    conn = kb.connect()
    try:
        work_id = kb.create_task(
            conn,
            title="XLSX 생성",
            assignee="docslead",
            initial_status="running",
        )
        assert kb.complete_task(
            conn,
            work_id,
            summary=f"파일 생성: {artifact}",
            metadata={"artifacts": [str(artifact)]},
        )
        review_id = kb.create_task(
            conn,
            title="[review] XLSX 최종 검수",
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
            reply_to_message_id="981",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review_status=pass, intent_source=telethon",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": "검수 통과했습니다. 파일을 첨부합니다.",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"] == "검수 통과했습니다. 파일을 첨부합니다."
    assert len(adapter.documents) == 1
    assert adapter.documents[0]["file_path"] == str(artifact)
    assert adapter.documents[0]["reply_to"] == "981"


def test_yuri_review_completion_delivers_approved_file_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-review-approved-file-artifact.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    artifact = tmp_path / "approved.xlsx"
    artifact.write_bytes(b"xlsx")

    conn = kb.connect()
    try:
        review_id = kb.create_task(
            conn,
            title="[review] XLSX 최종 검수",
            body="review_status metadata required. Check Telegram/Telethon intent.",
            assignee="reviewer",
            initial_status="running",
        )
        kb.add_notify_sub(
            conn,
            task_id=review_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="983",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review_status=pass, intent_source=telethon",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": "검수 통과했습니다. 수정본 파일을 첨부합니다.",
                "approved_file": str(artifact),
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"] == "검수 통과했습니다. 수정본 파일을 첨부합니다."
    assert len(adapter.documents) == 1
    assert adapter.documents[0]["file_path"] == str(artifact)
    assert adapter.documents[0]["reply_to"] == "983"


def test_telegram_artifact_delivery_suppresses_json_when_markdown_exists(tmp_path):
    json_artifact = tmp_path / "evidence.json"
    md_artifact = tmp_path / "evidence.md"
    json_artifact.write_text('{"internal": true}', encoding="utf-8")
    md_artifact.write_text("# Evidence\nReadable summary", encoding="utf-8")

    adapter = RecordingAdapter()
    adapter.name = "telegram"
    runner = _make_runner(adapter)

    delivered = asyncio.run(
        runner._deliver_kanban_artifacts(
            adapter=adapter,
            chat_id="chat-1",
            metadata={},
            event_payload={"artifacts": [str(json_artifact), str(md_artifact)]},
            task=None,
            reply_to="984",
            board_slug=None,
        )
    )

    assert delivered == 1
    assert len(adapter.documents) == 1
    assert adapter.documents[0]["file_path"] == str(md_artifact)
    assert adapter.documents[0]["reply_to"] == "984"


def test_yuri_file_claim_without_artifact_is_held_for_rework(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-missing-artifact-guard.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        work_id = kb.create_task(
            conn,
            title="XLSX 생성",
            assignee="docslead",
            initial_status="running",
        )
        assert kb.complete_task(conn, work_id, summary="파일 생성 검토 완료")
        review_id = kb.create_task(
            conn,
            title="[review] XLSX 최종 검수",
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
            reply_to_message_id="982",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review_status=pass, intent_source=telethon",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": "수정본 파일입니다. final.xlsx를 첨부합니다.",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.documents == []
    conn = kb.connect()
    try:
        rows = conn.execute(
            "SELECT title, assignee, status FROM tasks WHERE created_by = 'yuri-review-loop'"
        ).fetchall()
        old_subs = kb.list_notify_subs(conn, review_id)
    finally:
        conn.close()
    assert old_subs == []
    assert any(row["assignee"] == "docslead" and row["status"] == "ready" for row in rows)
    assert any(row["assignee"] == "reviewer" and row["status"] == "todo" for row in rows)


def test_yuri_file_claim_detector_does_not_match_pilot_wording():
    runner = _make_runner(RecordingAdapter())

    assert runner._yuri_completion_claims_file_delivery("파일을 첨부합니다.") is True
    assert runner._yuri_completion_claims_file_delivery("final.xlsx를 첨부합니다.") is True
    assert runner._yuri_completion_claims_file_delivery("파일럿 권고입니다.") is False
    assert runner._yuri_completion_claims_file_delivery("PDF 문서 업무에는 제한 파일럿이 맞습니다.") is False
    assert runner._yuri_completion_claims_file_delivery("deep-interview/SKILL.md가 맞습니다.") is False


def test_yuri_data_trust_gate_skips_github_skill_adoption_review():
    instruction = (
        "YURI secretary intake from Telegram.\n"
        "Original user text:\n"
        "https://github.com/devbrother2024/skills/blob/main/deep-interview/SKILL.md "
        "딥 인터뷰는 이게 아닌가요?\n\n"
        "Data trust metadata requirements for data/evidence work:\n"
        "- Reviewer pass is not enough by itself."
    )
    result = (
        "Telethon message_id=23480 readback, GitHub raw SKILL.md live fetch, "
        "검증 스크립트 통과를 확인했습니다."
    )

    assert GatewayRunner._yuri_data_trust_gate_required(instruction, result) is False


def test_yuri_artifact_status_resolves_relative_workspace_file(tmp_path, monkeypatch):
    db_path = tmp_path / "artifact-relative.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "deep_interview_final_text_ko.txt"
    artifact.write_text("approved", encoding="utf-8")
    runner = _make_runner(RecordingAdapter())
    task = type("Task", (), {"id": "t_relative", "workspace_path": str(workspace)})()

    status = runner._kanban_artifact_status(
        adapter=RecordingAdapter(),
        event_payload={"approved_file": artifact.name},
        task=task,
    )

    assert status["missing"] == []


def test_yuri_data_trust_allows_stale_evidence_output_path_when_no_file_delivery():
    instruction = (
        "Yuri review cycle follow-up review.\n"
        "Review the rework result against the original Telegram/Telethon intent.\n"
        "https://github.com/devbrother2024/skills/blob/main/deep-interview/SKILL.md"
    )
    event_payload = {
        "review_status": "pass",
        "intent_source": "telethon",
        "data_trust_status": "pass",
        "approved_final_text": "대표님, 보내주신 링크가 Deep Interview 맞습니다.",
        "source_files": ["/tmp/missing-evidence.json"],
        "output_path": "/tmp/missing-output.json",
        "verification_commands": ["live GitHub raw fetch"],
        "counts": {"skill_raw_http_status": 200},
        "remaining_risks": ["GitHub raw content can change."],
    }

    issue = GatewayRunner._yuri_data_trust_gate_issue(
        instruction,
        event_payload["approved_final_text"],
        event_payload=event_payload,
        artifact_status={"missing": ["/tmp/missing-output.json"]},
    )

    assert issue is None


def test_yuri_data_trust_accepts_reviewer_checks_output_counts_and_singular_risk():
    instruction = (
        "YURI secretary intake from Telegram.\n"
        "Original user text:\n"
        "84행 자동검사 통과 예외 분리 결과를 검증해주세요."
    )
    event_payload = {
        "review_status": "pass",
        "intent_source": "telethon",
        "approved_final_text": "84행 자동검사 결과입니다.",
        "checks": ["manifest readback", "script rerun", "source hash unchanged"],
        "output_counts": {"input_candidates": 84, "auto_pass": 84, "exceptions": 0},
        "remaining_risk": "공식 그래프 반영은 별도 단계입니다.",
    }

    issue = GatewayRunner._yuri_data_trust_gate_issue(
        instruction,
        event_payload["approved_final_text"],
        event_payload=event_payload,
    )

    assert issue is None


def test_yuri_data_trust_blocks_missing_declared_delivery_artifact():
    instruction = (
        "YURI secretary intake from Telegram.\n"
        "Original user text:\n"
        "후보자 선수 데이터가 맞는지 재검증해주세요."
    )
    event_payload = {
        "review_status": "pass",
        "intent_source": "telethon",
        "data_trust_status": "pass",
        "approved_final_text": "후보자 선수 데이터 재검증 결과입니다. 파일을 첨부합니다.",
        "approved_file": "/tmp/missing-final.xlsx",
        "verification_commands": ["openpyxl readback"],
        "counts": {"rows_checked": 10},
        "remaining_risks": ["수동확인 필요"],
    }

    issue = GatewayRunner._yuri_data_trust_gate_issue(
        instruction,
        event_payload["approved_final_text"],
        event_payload=event_payload,
        artifact_status={"missing": ["/tmp/missing-final.xlsx"]},
    )

    assert "데이터 산출물로 선언한 파일" in issue


def test_yuri_data_review_pass_without_trust_evidence_is_held(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-data-trust-missing.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        work_id = kb.create_task(
            conn,
            title="[YURI intake] 후보자 선수 데이터 재검증",
            body=(
                "YURI secretary intake from Telegram.\n"
                "Original user text:\n"
                "후보자 선수 데이터가 맞는지 재검증해주세요."
            ),
            assignee="analyst",
            initial_status="running",
        )
        assert kb.complete_task(conn, work_id, summary="후보자 선수 데이터 재검증 초안입니다.")
        review_id = kb.create_task(
            conn,
            title="[review] 후보자 선수 데이터 재검증 검수",
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
            reply_to_message_id="984",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review_status=pass, intent_source=telethon",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": "후보자 선수 데이터 재검증을 완료했습니다.",
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
            "SELECT title, assignee, status, body FROM tasks WHERE created_by = 'yuri-review-loop'"
        ).fetchall()
        old_subs = kb.list_notify_subs(conn, review_id)
    finally:
        conn.close()
    assert old_subs == []
    assert any(row["assignee"] == "analyst" and row["status"] == "ready" for row in rows)
    assert any(row["assignee"] == "reviewer" and row["status"] == "todo" for row in rows)
    assert any("데이터 신뢰도 증거가 부족합니다" in row["body"] for row in rows)


def test_yuri_data_review_pass_with_trust_evidence_delivers(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-data-trust-pass.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        review_id = kb.create_task(
            conn,
            title="[review] 후보자 선수 데이터 재검증 검수",
            body=(
                "YURI secretary intake from Telegram.\n"
                "Original user text:\n"
                "후보자 선수 데이터가 맞는지 재검증해주세요."
            ),
            assignee="reviewer",
            initial_status="running",
        )
        kb.add_notify_sub(
            conn,
            task_id=review_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="985",
        )
        assert kb.complete_task(
            conn,
            review_id,
            summary="review_status=pass, intent_source=telethon",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "data_trust_status": "pass",
                "approved_final_text": "후보자 선수 데이터 재검증 결과입니다.",
                "evidence_checked": ["선관위 원천 CSV", "기존 수정본 XLSX"],
                "verification_commands": ["openpyxl readback"],
                "counts": {"rows_checked": 3},
                "sample_rows": [{"name": "추미애", "term": "7선"}],
                "sha256_16": "0123456789abcdef",
                "remaining_risks": ["비공개 경력은 자동 확정 불가"],
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"] == "후보자 선수 데이터 재검증 결과입니다."


def test_yuri_notifier_preserves_linebreaks_in_approved_final_text(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-linebreak-approved-final.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    final_text = "대표님, 정리드립니다.\n\n- 첫 항목입니다.\n- 둘째 항목입니다."

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="[YURI intake] 줄바꿈해서 다시 답변",
            body="YURI secretary intake from Telegram.\nOriginal user text:\n줄바꿈해서 다시 답변해주세요.",
            assignee="planner",
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
                "approved_final_text": final_text,
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"] == final_text
    assert "\n\n- 첫 항목" in adapter.sent[0]["text"]


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


def test_kanban_notifier_hands_off_verified_cards_even_without_handoff_words(tmp_path, monkeypatch):
    db_path = tmp_path / "verified-card-english-graph-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn,
            title="[YURI intake] 진행해주세요",
            assignee="planner",
            session_id="session-planner",
        )
        source_id = kb.create_task(
            conn,
            title="[source-map] 84행 자동검사기 입력 확인",
            assignee="knowledge-librarian",
            created_by="planner",
            parents=[parent_id],
        )
        work_id = kb.create_task(
            conn,
            title="[implement] 84행 자동검사 통과/예외 분리기 구현 및 실행",
            assignee="ops",
            created_by="planner",
            parents=[source_id],
        )
        review_id = kb.create_task(
            conn,
            title="[review] 84행 자동검사기 결과·의도·최종문안 검수",
            assignee="reviewer",
            created_by="planner",
            parents=[work_id],
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="39886",
        )
        kb.complete_task(
            conn,
            parent_id,
            summary=(
                "review_status=pass, intent_source=telethon: "
                "Created a three-step dependency graph: source-map, implementation, reviewer gate."
            ),
            created_cards=[source_id, work_id, review_id],
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
        review_subs = kb.list_notify_subs(conn, review_id)
        intermediate_subs = kb.list_notify_subs(conn, source_id) + kb.list_notify_subs(conn, work_id)
    finally:
        conn.close()
    assert parent_subs == []
    assert intermediate_subs == []
    assert len(review_subs) == 1
    assert review_subs[0]["reply_to_message_id"] == "39886"


def test_kanban_notifier_hands_off_assignment_to_final_report_grandchild(tmp_path, monkeypatch):
    db_path = tmp_path / "assignment-final-grandchild-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn,
            title="[YURI intake] 지금 여유있어 정리해줘",
            assignee="planner",
            session_id="session-planner",
        )
        source_id = kb.create_task(
            conn,
            title="[정리-소스맵] 보류/대기 목록 확인",
            assignee="knowledge-librarian",
            created_by="planner",
            parents=[parent_id],
        )
        cleanup_id = kb.create_task(
            conn,
            title="[정리-작업판] 안전 정리",
            assignee="ops",
            created_by="planner",
            parents=[source_id],
        )
        review_id = kb.create_task(
            conn,
            title="[검수] 작업판 정리 결과 검수",
            assignee="reviewer",
            created_by="planner",
            parents=[cleanup_id],
        )
        final_id = kb.create_task(
            conn,
            title="[최종검수/보고문] 현재 여유시간 정리 작업 결과 종합 보고",
            assignee="reviewer",
            created_by="planner",
            parents=[review_id],
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="39798",
        )
        kb.complete_task(
            conn,
            parent_id,
            summary=(
                "review_status=pass, intent_source=telethon: "
                "소스맵→작업판 정리→검수→최종 보고문 카드로 배정했습니다."
            ),
            created_cards=[source_id, cleanup_id, review_id, final_id],
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
        final_subs = kb.list_notify_subs(conn, final_id)
        intermediate_subs = (
            kb.list_notify_subs(conn, source_id)
            + kb.list_notify_subs(conn, cleanup_id)
            + kb.list_notify_subs(conn, review_id)
        )
    finally:
        conn.close()
    assert parent_subs == []
    assert intermediate_subs == []
    assert len(final_subs) == 1
    assert final_subs[0]["reply_to_message_id"] == "39798"


def test_kanban_notifier_hands_off_to_completed_final_report_on_late_tick(tmp_path, monkeypatch):
    db_path = tmp_path / "late-final-report-handoff-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn,
            title="[YURI intake] 지금 여유있어 정리해줘",
            assignee="planner",
            session_id="session-planner",
        )
        work_id = kb.create_task(
            conn,
            title="[정리-작업판] 안전 정리",
            assignee="ops",
            created_by="planner",
            parents=[parent_id],
        )
        final_id = kb.create_task(
            conn,
            title="[최종검수/보고문] 현재 여유시간 정리 작업 결과 종합 보고",
            assignee="reviewer",
            created_by="planner",
            parents=[work_id],
        )
        kb.add_notify_sub(
            conn,
            task_id=parent_id,
            platform="telegram",
            chat_id="chat-1",
            reply_to_message_id="39798",
        )
        kb.complete_task(
            conn,
            parent_id,
            summary=(
                "review_status=pass, intent_source=telethon: "
                "작업판 정리→최종 보고문 카드로 배정했습니다."
            ),
            created_cards=[work_id, final_id],
        )
        kb.complete_task(conn, work_id, summary="정리 완료")
        kb.complete_task(
            conn,
            final_id,
            summary="최종 보고문 검수 완료",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
                "approved_final_text": "대표님, 최종 보고입니다.",
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
        assert kb.list_notify_subs(conn, parent_id) == []
        final_subs = kb.list_notify_subs(conn, final_id)
    finally:
        conn.close()
    assert len(final_subs) == 1
    assert final_subs[0]["reply_to_message_id"] == "39798"

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["text"] == "대표님, 최종 보고입니다."
    assert adapter.sent[0]["reply_to"] == "39798"


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
    """A retry cycle keeps recovery alive without repeatedly nudging chat."""
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

    assert len(adapter.sent) == 1
    assert "중간에 멈췄습니다" in adapter.sent[0]["text"]
    assert "다시 시도" not in adapter.sent[0]["text"]
    assert "재시도" not in adapter.sent[0]["text"]

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so recovery can keep "
            "running even when repeated retry chatter is suppressed."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert _unseen_terminal_events(tid) == []


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
