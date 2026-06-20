import json

from gateway import yuri_knowledge_spine as spine


def _read_events(path):
    events_path = path / "events.jsonl"
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]


def test_yuri_knowledge_spine_renders_context_pack_and_records_events(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path))

    pack = spine.build_context_pack(
        original_user_text="코드 오류 원인을 확인하고 수정해주세요.",
        platform="telegram",
        message_id="m-1",
    )
    rendered = spine.render_context_pack(pack)

    assert "YURI KNOWLEDGE SPINE CONTEXT PACK" in rendered
    assert "코드 오류 원인을 확인하고 수정해주세요." in rendered
    assert "required_review_status: pass" in rendered
    assert "telethon" in rendered

    spine.record_intake(pack, task_id="t_root")
    spine.record_review_result(
        root_task_id="t_root",
        reviewer_task_id="t_review",
        approved_final_text="검수 통과 문안입니다.",
        intent_source="telethon",
        board="telegram-inbox",
    )

    rows = _read_events(tmp_path)
    assert [row["kind"] for row in rows] == ["yuri_intake", "review_finalized"]
    assert rows[0]["payload"]["task_id"] == "t_root"
    assert rows[1]["payload"]["approved_final_text"] == "검수 통과 문안입니다."

    graph_raw = spine.export_graph_jsonl(limit=20)
    graph_edges = [json.loads(line) for line in graph_raw.splitlines()]
    relations = {edge["relation"] for edge in graph_edges}
    assert "HAS_USER_INTENT" in relations
    assert "REQUIRES_REVIEW_STATUS" in relations
    assert "REVIEWED_BY" in relations
    assert "APPROVED_WITH" in relations
    assert "INTENT_SOURCE" in relations

    report = spine.build_graph_export_report("코드 오류", limit=10)
    assert "Yuri graph export" in report
    assert "HAS_USER_INTENT" in report
    assert "task:t_root" in report

    learned = spine.recall_lessons("코드 오류 원인", limit=3)
    assert learned
    assert learned[0]["root_task_id"] == "t_root"

    followup = spine.build_context_pack(
        original_user_text="코드 오류 원인 다시 확인해주세요.",
        platform="telegram",
    )
    rendered_followup = spine.render_context_pack(followup)
    assert followup["learned_patterns"]
    assert "learned_patterns" in rendered_followup
    assert "review_status=pass" in rendered_followup

    learning_report = spine.build_learning_report("코드 오류", limit=3)
    assert "Yuri learning report" in learning_report
    assert "t_root" in learning_report


def test_yuri_knowledge_spine_includes_recent_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path))

    first = spine.build_context_pack(
        original_user_text="텔레쏜 대화 기준으로 원인을 확인해주세요.",
        platform="telegram",
    )
    spine.record_intake(first, task_id="t_old")

    second = spine.build_context_pack(
        original_user_text="다음 작업도 같은 기준으로 확인해주세요.",
        platform="telegram",
    )

    assert second["recent_spine_events"]
    assert "t_old" in second["recent_spine_events"][0]


def test_yuri_failure_cases_seed_context_and_regression_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path))

    failure = spine.record_failure_case(
        user_text="유리가 줄바꿈도 못하고 파일 보냈다고 하는데 첨부가 없습니다.",
        observed_behavior="User reported unreadable Telegram output and missing attachment evidence.",
        source="test",
        message_id="m-fail",
    )

    assert failure["category"] in {"file_claim_without_media", "linebreak_or_readability"}
    failures_path = tmp_path / "failures.jsonl"
    regression_path = tmp_path / "regression_candidates.jsonl"
    assert failures_path.is_file()
    assert regression_path.is_file()

    pack = spine.build_context_pack(
        original_user_text="파일 보냈다는 보고가 맞는지 다시 확인해주세요.",
        platform="telegram",
    )
    rendered = spine.render_context_pack(pack)
    assert pack["failure_patterns"]
    assert "failure_patterns_to_avoid" in rendered

    report = spine.build_learning_report("파일 첨부", limit=5)
    assert "matched_failure_patterns" in report
    assert "regression_candidates" in report
    assert "forbidden" in report


def test_yuri_failure_case_tracks_followup_context_drift(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path))

    failure = spine.record_failure_case(
        user_text="방금 맥락을 이해못하고 줄바꿈 얘기에서 전체 운영 개선으로 벗어났습니다.",
        observed_behavior="Follow-up question after linebreak tuning was answered as broad system optimization.",
        source="test",
    )

    assert failure["category"] == "context_drift_followup"
    assert "직전 주제" in failure["expected_behavior"]

    pack = spine.build_context_pack(
        original_user_text="줄바꿈 말고 또 개선할 게 뭐가 있을까?",
        platform="telegram",
    )
    rendered = spine.render_context_pack(pack)
    assert "context_drift_followup" in rendered
    assert "직전 주제" in rendered


def test_yuri_failure_case_tracks_ai_processing_source_misroute(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path))

    failure = spine.record_failure_case(
        user_text="ai 처리가 로컬로 돌고있니? 아님 내 gpt로 진행중이니?",
        observed_behavior="Yuri answered generic Kanban status instead of preserving the poll collector AI-processing context.",
        source="test",
    )

    assert failure["category"] == "ai_processing_source_misrouted"
    assert "로컬/GPT/모델" in failure["expected_behavior"]

    pack = spine.build_context_pack(
        original_user_text="내 gpt로 진행중이니?",
        platform="telegram",
    )
    rendered = spine.render_context_pack(pack)
    assert "ai_processing_source_misrouted" in rendered
    assert "파이프라인 맥락" in rendered


def test_yuri_knowledge_spine_recalls_relevant_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path))

    poll = spine.build_context_pack(
        original_user_text="여론조사 PDF 인식 개선 상태를 확인해주세요.",
        platform="telegram",
    )
    spine.record_intake(poll, task_id="t_poll")
    spine.record_review_result(
        root_task_id="t_poll",
        reviewer_task_id="t_review_poll",
        approved_final_text="여론조사 PDF 인식은 5개 샘플 테스트를 통과했습니다.",
        intent_source="telethon",
        board="telegram-inbox",
    )

    unrelated = spine.build_context_pack(
        original_user_text="캘린더 회의 일정을 확인해주세요.",
        platform="telegram",
    )
    spine.record_intake(unrelated, task_id="t_calendar")

    recalled = spine.recall_relevant_events("여론조사 인식 개선은 어떻게 되고있나요?", limit=2)
    rendered = spine.render_context_pack(
        spine.build_context_pack(
            original_user_text="여론조사 인식 개선은 어떻게 되고있나요?",
            platform="telegram",
        )
    )

    assert recalled
    assert any("t_poll" in spine._summarize_event(row) for row in recalled)
    assert "relevant_spine_events" in rendered
    assert "t_poll" in rendered


def test_yuri_memory_audit_report_includes_spine_and_kanban_trace(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="[YURI intake] 여론조사 인식 개선",
            body="YURI secretary intake from Telegram.",
            assignee="planner",
            initial_status="running",
        )
        kb.complete_task(
            conn,
            task_id,
            result="여론조사 PDF 인식은 샘플 테스트를 통과했습니다.",
            summary="검수 통과",
            metadata={
                "review_status": "pass",
                "intent_source": "telethon",
            },
        )
    finally:
        conn.close()

    pack = spine.build_context_pack(
        original_user_text="여론조사 인식 개선은 어떻게 되고있나요?",
        platform="telegram",
    )
    spine.record_intake(pack, task_id=task_id)

    report = spine.build_audit_report("여론조사 인식 개선", limit=3)

    assert "Yuri memory audit" in report
    assert "relevant_spine_events" in report
    assert task_id in report
    assert "kanban_trace" in report
    assert "review_status=pass" in report
    assert "intent_source=telethon" in report


def test_yuri_knowledge_spine_exports_okf_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))

    pack = spine.build_context_pack(
        original_user_text="텔레쏜 대화 기준으로 유리 원인을 확인해주세요.",
        platform="telegram",
        message_id="m-okf",
    )
    spine.record_intake(pack, task_id="t_okf")
    spine.record_review_result(
        root_task_id="t_okf",
        reviewer_task_id="t_review_okf",
        approved_final_text="검수팀 확인 후 의도가 반영되었습니다.",
        intent_source="telethon",
        board="telegram-inbox",
    )

    out_dir = tmp_path / "bundle"
    result = spine.export_okf_bundle(output_dir=out_dir)
    report = spine.build_okf_export_report(output_dir=out_dir)

    assert result["okf_version"] == "0.1"
    assert result["events_exported"] == 2
    assert result["tasks_exported"] == 1
    assert (out_dir / "index.md").is_file()
    assert (out_dir / "log.md").is_file()
    assert (out_dir / "events" / "index.md").is_file()
    assert (out_dir / "tasks" / "t_okf.md").is_file()
    assert "okf_version: \"0.1\"" in (out_dir / "index.md").read_text(encoding="utf-8")
    assert "type: \"Yuri Task\"" in (out_dir / "tasks" / "t_okf.md").read_text(encoding="utf-8")
    assert "Yuri OKF export" in report
    assert "conformance" in report


def test_yuri_knowledge_spine_exports_graphiti_episodes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))

    pack = spine.build_context_pack(
        original_user_text="파일 첨부 누락을 반복하지 않게 해주세요.",
        platform="telegram",
        message_id="m-graphiti",
    )
    spine.record_intake(pack, task_id="t_graphiti")
    spine.record_review_result(
        root_task_id="t_graphiti",
        reviewer_task_id="t_review_graphiti",
        approved_final_text="첨부 누락 방지 회귀 테스트를 추가했습니다.",
        intent_source="telethon",
        board="telegram-inbox",
    )

    out = tmp_path / "graphiti.jsonl"
    result = spine.export_graphiti_episodes(output_path=out, group_id="yuri-test")
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

    assert result["schema_version"] == "yuri-graphiti-episodes-v1"
    assert result["episodes_exported"] == 2
    assert len(lines) == 2
    assert lines[0]["graphiti_tool"] == "add_memory"
    assert lines[0]["arguments"]["source"] == "json"
    assert lines[0]["arguments"]["group_id"] == "yuri-test"
    assert "episode_body" in lines[0]["arguments"]
