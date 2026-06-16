from gateway.kanban_watchers import repair_yuri_missing_profile_assignees
from hermes_cli import kanban_db as kb


def _task_assignee(conn, task_id):
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["assignee"]


def test_yuri_pseudo_assignees_repair_to_real_profiles(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-assignee-repair.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        poll_id = kb.create_task(
            conn,
            title="[여론조사 수집기] 런타임 점검",
            body="상위 요청: Telegram 원문은 “여론조사 수집기는 잘 돌고있는지 확인해주세요.”입니다.",
            assignee="collection-lead",
        )
        waiting_parent_id = kb.create_task(
            conn,
            title="waiting parent",
            assignee="planner",
            initial_status="blocked",
        )
        fact_id = kb.create_task(
            conn,
            title="[후속] 기사 수집 품질 검증",
            body="대표님 원문(telethon source): “어떤 후속작업이죠? 진행해주세요.”",
            assignee="factcheck-lead",
            parents=[waiting_parent_id],
        )
        normal_id = kb.create_task(
            conn,
            title="ordinary queued work",
            body="This is not Yuri or Telegram intake.",
            assignee="collection-lead",
        )

        repaired = repair_yuri_missing_profile_assignees(
            conn,
            profile_exists=lambda name: name in {"ops", "researcher"},
            assign_task=kb.assign_task,
        )

        assert repaired == [
            (poll_id, "collection-lead", "ops"),
            (fact_id, "factcheck-lead", "researcher"),
        ]
        assert _task_assignee(conn, poll_id) == "ops"
        assert _task_assignee(conn, fact_id) == "researcher"
        assert _task_assignee(conn, normal_id) == "collection-lead"
    finally:
        conn.close()


def test_yuri_assignee_repair_skips_missing_target_profile(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-assignee-repair-missing-target.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="[YURI intake] 커뮤니티 수집기 점검",
            body="YURI secretary intake from Telegram.",
            assignee="collection-lead",
        )

        repaired = repair_yuri_missing_profile_assignees(
            conn,
            profile_exists=lambda name: False,
            assign_task=kb.assign_task,
        )

        assert repaired == []
        assert _task_assignee(conn, task_id) == "collection-lead"
    finally:
        conn.close()


def test_yuri_article_quality_collection_lead_repairs_to_researcher(tmp_path, monkeypatch):
    db_path = tmp_path / "yuri-article-quality-assignee-repair.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="[audit] 기사수집기 현재 퀄리티 근거 기반 평가",
            body="상위 질문: “기사수집기 퀄리티는 어떤가요?”",
            assignee="collection-lead",
        )

        repaired = repair_yuri_missing_profile_assignees(
            conn,
            profile_exists=lambda name: name in {"ops", "researcher"},
            assign_task=kb.assign_task,
        )

        assert repaired == [(task_id, "collection-lead", "researcher")]
        assert _task_assignee(conn, task_id) == "researcher"
    finally:
        conn.close()
