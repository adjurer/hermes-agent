from hermes_cli import yuri_quality as yq
import sqlite3


def test_yuri_quality_detects_file_claim_without_media():
    issues = yq.scan_messages([
        {"id": "1", "sender": "YURI", "text": "파일 전송 완료했습니다.", "has_media": False}
    ])

    assert {i.code for i in issues} == {"file_claim_without_media"}


def test_yuri_quality_detects_local_delivery_noise():
    issues = yq.scan_messages([
        {"id": "2", "sender": "YURI", "text": "저장은 /Users/tbd/tmp/report.md 입니다."}
    ])

    assert "local_delivery_noise" in {i.code for i in issues}


def test_yuri_quality_detects_worker_direct_report():
    issues = yq.scan_messages([
        {"id": "3", "sender": "문서제작실", "text": "전송 완료했습니다, 대표님."}
    ])

    codes = {i.code for i in issues}
    assert "worker_direct_report" in codes
    assert "file_claim_without_media" in codes


def test_yuri_quality_detects_context_loss_despite_recent_hint():
    issues = yq.scan_messages([
        {"id": "4a", "sender": "대표님", "text": "Scrapling도 합친 조합으로 할수있어?"},
        {"id": "4b", "sender": "YURI", "text": "네, Scrapling 조합으로 가능합니다."},
        {"id": "4c", "sender": "대표님", "text": "네 조합으로 진행해주세요."},
        {"id": "4d", "sender": "YURI", "text": "맥락을 먼저 확인해야 합니다. 연결할 진행 업무를 찾지 못했습니다."},
    ])

    assert "context_lost_despite_recent_hint" in {i.code for i in issues}


def test_yuri_quality_detects_raw_cron_failure():
    issues = yq.scan_messages([
        {"id": "5", "sender": "YURI", "text": "⚠️ Cron job 'poll' failed: Script exited with code 255"}
    ])

    assert "raw_failure_leaked" in {i.code for i in issues}


def test_yuri_quality_detects_simple_check_overrouting():
    issues = yq.scan_messages([
        {"id": "6a", "sender": "대표님", "text": "유리야 진단 응답 테스트입니다. OK라고만 답해줘."},
        {"id": "6b", "sender": "YURI", "text": "네. 운영팀에 이관하겠습니다."},
    ])

    assert "simple_check_overrouted" in {i.code for i in issues}


def test_yuri_quality_detects_thin_orchestration_ack():
    issues = yq.scan_messages([
        {"id": "7", "sender": "YURI", "text": "네. 운영팀에 이관하겠습니다."}
    ])

    assert "thin_orchestration_ack" in {i.code for i in issues}


def test_yuri_quality_detects_stock_orchestration_phrase():
    issues = yq.scan_messages([
        {
            "id": "8",
            "sender": "YURI",
            "text": "진행과 검증은 나눠 진행하고, 결과는 제가 모아서 짧게 보고드리겠습니다.",
        }
    ])

    assert "stock_orchestration_phrase" in {i.code for i in issues}


def test_yuri_quality_backtests_pass():
    result = yq.run_backtests()

    assert result["ok"] is True


def test_yuri_quality_inventory_tracks_skill_visibility(tmp_path):
    home = tmp_path / ".hermes"
    repo = home / "hermes-agent"
    root_skill = home / "skills" / "productivity" / "root-only"
    shared_skill = home / "skills" / "devops" / "shared-skill"
    profile_skill = home / "profiles" / "planner" / "skills" / "devops" / "shared-skill"
    profile_only_skill = home / "profiles" / "planner" / "skills" / "research" / "profile-only"
    for path, name in [
        (root_skill, "root-only"),
        (shared_skill, "shared-skill"),
        (profile_skill, "shared-skill"),
        (profile_only_skill, "profile-only"),
    ]:
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    planner = home / "profiles" / "planner"
    planner.mkdir(parents=True, exist_ok=True)
    (planner / "profile.yaml").write_text("name: planner\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "skills:\n"
        "  auto_inject:\n"
        "    coding:\n"
        "      enabled: true\n"
        "      skills:\n"
        "        - shared-skill\n"
        "  platform_disabled:\n"
        "    telegram:\n"
        "      - root-only\n",
        encoding="utf-8",
    )
    db_path = home / "kanban" / "boards" / "telegram-inbox" / "kanban.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT, title TEXT, skills TEXT, created_at INTEGER, completed_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            ("t_recent", "recent shared skill use", '["shared-skill"]', 123, 456),
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            ("t_gap", "recent missing worker use", '["missing-worker"]', 124, 457),
        )
        conn.commit()
    finally:
        conn.close()

    inventory = yq.build_inventory(hermes_home=home, repo_root=repo)
    rows = {row["name"]: row for row in inventory["skill_visibility"]}

    assert rows["root-only"]["root_available"] is True
    assert rows["root-only"]["profiles"] == []
    assert rows["root-only"]["missing_profiles"] == ["planner"]
    assert rows["root-only"]["platform_disabled"] == ["telegram"]
    assert rows["shared-skill"]["profiles"] == ["planner"]
    assert rows["shared-skill"]["auto_inject_groups"] == ["coding"]
    assert rows["shared-skill"]["recent_use"]["latest_task_id"] == "t_recent"
    assert rows["profile-only"]["root_available"] is False
    assert rows["profile-only"]["sources"] == ["profile:planner"]
    assert rows["profile-only"]["profiles"] == ["planner"]
    assert rows["profile-only"]["missing_profiles"] == []
    assert inventory["recent_skill_usage"]["shared-skill"]["count"] == 1
    assert inventory["skill_reference_gaps"] == [
        {
            "name": "missing-worker",
            "reason": "recent_task_skill_not_installed",
            "recent_use": {
                "count": 1,
                "latest_board": "telegram-inbox",
                "latest_completed_at": 457,
                "latest_created_at": 124,
                "latest_task_id": "t_gap",
                "latest_title": "recent missing worker use",
            },
        }
    ]
    assert inventory["skill_visibility_summary"]["skills_missing_from_any_profile"] == 1
    assert inventory["skill_visibility_summary"]["recent_skill_reference_gaps"] == 1
