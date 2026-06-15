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
