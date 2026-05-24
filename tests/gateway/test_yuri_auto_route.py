from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import (
    GatewayRunner,
    _kanban_text_claims_artifact_delivery,
    _secretary_clean_kanban_text,
)
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


def _runner(profile: str = "default") -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._active_profile_name = lambda: profile
    return runner


class _TranscriptStore:
    def __init__(self, session_id="session-ctx"):
        self.session_id = session_id
        self.entries = []
        self.updated = []

    def get_or_create_session(self, source):
        return SimpleNamespace(session_id=self.session_id, session_key="key-ctx")

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.entries.append((session_id, message))

    def update_session(self, session_key, last_prompt_tokens=None):
        self.updated.append(session_key)


def _event(
    text: str,
    *,
    media: bool = False,
    is_bot: bool = False,
    message_type: MessageType = MessageType.TEXT,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="c1",
            user_id="u1",
            user_name="tester",
            chat_type="dm",
            is_bot=is_bot,
        ),
        message_id="m1",
        media_urls=["/tmp/example.png"] if media else [],
        media_types=["image/png"] if media else [],
    )


def test_yuri_auto_route_ignores_short_context_followups():
    assert _runner()._classify_yuri_auto_route(_event("네 진행해주세요")) is None


def test_yuri_auto_route_keeps_plain_questions_in_chat():
    assert _runner()._classify_yuri_auto_route(_event("큐드라이버가 대체 뭔가요?")) is None


def test_yuri_auto_route_keeps_capability_questions_in_chat_even_with_action_words():
    assert _runner()._classify_yuri_auto_route(
        _event("스프레드시트가 업데이트 되면 트리거가 되어서 바로 지도에 반영할수없어?")
    ) is None


def test_yuri_auto_route_keeps_ordinal_followup_in_chat():
    assert _runner()._classify_yuri_auto_route(
        _event("1번에 대해 더 자세하게 보고해")
    ) is None


def test_yuri_auto_route_keeps_understanding_checks_in_chat():
    assert _runner()._classify_yuri_auto_route(
        _event("[테스트] 파일 만들지 말고 메시지 이해 여부만 확인해주세요")
    ) is None


def test_yuri_auto_route_keeps_status_followup_questions_in_chat():
    assert _runner()._classify_yuri_auto_route(_event("찾아봤니?")) is None
    assert _runner()._classify_yuri_auto_route(_event("어디까지 됐어?")) is None


def test_yuri_auto_route_keeps_question_style_data_checks_in_chat():
    assert _runner()._classify_yuri_auto_route(
        _event("클린버전으로 다시 수집하기로 했었는데 클린버전으로 수집된 데이터인가요?")
    ) is None


def test_yuri_auto_route_does_not_blind_route_voice_only_messages():
    assert _runner()._classify_yuri_auto_route(
        _event("", media=True, message_type=MessageType.VOICE)
    ) is None


def test_yuri_auto_route_recent_error_questions_go_to_review():
    route = _runner()._classify_yuri_auto_route(
        _event("방금 난 오류 알림은 뭐야?")
    )

    assert route is not None
    assert route["assignee"] == "reviewer"
    assert "최근 Telegram 메시지, cron 상태, gateway 로그" in route["body"]


def test_yuri_auto_route_sends_ops_work_to_ops():
    route = _runner()._classify_yuri_auto_route(
        _event("맥서버 상태 점검하고 게이트웨이 로그 확인해주세요")
    )

    assert route is not None
    assert route["assignee"] == "ops"
    assert "유리 자동 라우팅" in route["body"]
    assert "유리가 대표님께 설명한 해석" in route["body"]
    assert "작업자 실행 해석" in route["body"]
    assert "HTTP/API/파일/DB/CLI" in route["body"]
    assert "리스크가 큰 경우에만" in route["body"]
    assert "대표님 원문" not in route["body"]
    assert "그대로 인용" in route["body"]
    assert "접수" not in route["body"]


def test_yuri_auto_route_sends_address_lookup_to_ops_not_planner():
    route = _runner()._classify_yuri_auto_route(
        _event("주소 보내줘 아마 유나피시에서 개발하던게 있을꺼야.")
    )

    assert route is not None
    assert route["assignee"] == "ops"
    assert "접속 주소 후보" in route["body"]


def test_yuri_auto_route_sends_document_work_to_docslead():
    route = _runner()._classify_yuri_auto_route(
        _event("한글 파일을 만들어서 파일명 규칙에 맞게 보내주세요")
    )

    assert route is not None
    assert route["assignee"] == "docslead"
    assert "YYMMDD_한글파일명" in route["body"]


def test_yuri_auto_route_sends_community_data_collection_to_researcher():
    route = _runner()._classify_yuri_auto_route(
        _event("커뮤니티 데이터 26년 1월 1일 기준으로 모으는 것에 대해 검수하고 수집 진행해주세요")
    )

    assert route is not None
    assert route["assignee"] == "researcher"
    assert "라우팅 사유: 조사/검색" in route["body"]
    assert "원인/영향/다음 조치" in route["body"]


def test_yuri_auto_route_marks_untargeted_delivery_as_followup():
    route = _runner()._classify_yuri_auto_route(
        _event("이기형 후보의 사진도 빠져있다고 전달해줘")
    )

    assert route is not None
    assert route["active_work_followup"] is True
    assert "후속 요구사항" in route["body"]


def test_yuri_auto_route_complex_execution_becomes_pm_root():
    route = _runner()._classify_yuri_auto_route(
        _event(
            "63지선 방에 있는 스티커 이미지를 찾아서 공개 접속 안내 레이어에 넣고, "
            "만든이 문구는 더불어민주당 경기도당 김승원 의원실로 바꾸고, "
            "하얀 박스도 키운 다음 공개 화면에서 검수까지 진행해주세요",
            media=True,
        )
    )

    assert route is not None
    assert route["assignee"] == "planner"
    assert route["pm_root"] is True
    assert route["source_room_required"] is True
    assert "유리 PM 오케스트레이션 루트 업무" in route["body"]
    assert "telegram-user MCP" in route["body"]
    assert "reviewer는 기본으로 붙이지 않습니다" in route["body"]
    assert "첨부 작업자료" in route["body"]
    assert "/tmp/example.png" in route["body"]
    assert "작업자료로 같이 전달" in route["body"]


def test_yuri_auto_route_marks_contextual_followup():
    route = _runner()._classify_yuri_auto_route(
        _event("하얀 박스도 더 키워도 좋아")
    )

    assert route is not None
    assert route["contextual_followup"] is True
    assert "진행 중인 PM 루트" in route["body"]


def test_yuri_file_delivery_followup_requires_previous_context():
    route = _runner()._classify_yuri_auto_route(_event("파일로 보내주세요"))

    assert route is not None
    assert route["assignee"] == "docslead"
    assert route["contextual_followup"] is True
    assert route["delivery_followup"] is True
    assert route["needs_prior_context"] is True
    assert "실제 첨부 성공 전까지" in route["body"]


def test_yuri_file_negation_does_not_route_to_docslead():
    route = _runner()._classify_yuri_auto_route(
        _event("[테스트] 파일 만들지 말고 메시지 이해 여부만 확인해주세요")
    )

    assert route is None


def test_yuri_worker_brief_includes_followup_context_recovery_rules():
    route = _runner()._classify_yuri_auto_route(
        _event("맥서버 상태 점검하고 게이트웨이 로그 확인해주세요")
    )

    assert route is not None
    assert "방금/아까/1번/그거" in route["body"]
    assert "다른 사람의 봇" in route["body"]


def test_yuri_protocol_prompt_is_injected_for_telegram_default_profile():
    runner = _runner()
    prompt = runner._yuri_secretary_protocol_prompt(_event("방금 오류 뭐야?"), _event("x").source)

    assert "YURI secretary protocol" in prompt
    assert "마무리했습니다" in prompt
    assert "페이커, 올리비아" in prompt


def test_yuri_protocol_prompt_ignores_worker_profiles():
    runner = _runner("researcher")

    assert runner._yuri_secretary_protocol_prompt(_event("방금 오류 뭐야?"), _event("x").source) == ""


@pytest.mark.asyncio
async def test_yuri_followup_attaches_to_active_session_task(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn,
            title="최초 접속 페이지 수정",
            assignee="planner",
            session_id="session-1",
        )
    finally:
        conn.close()

    runner = _runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1")
    )
    route = runner._classify_yuri_auto_route(
        _event("이기형 후보의 사진도 빠져있다고 전달해줘")
    )

    response = await runner._route_yuri_message_to_kanban(
        _event("이기형 후보의 사진도 빠져있다고 전달해줘"),
        route,
    )

    assert response is not None
    assert "이어 붙" in response
    conn = kb.connect()
    try:
        children = kb.child_ids(conn, parent_id)
        assert children == []
        comments = kb.list_comments(conn, parent_id)
        assert comments
        assert "후속 요구사항" in comments[-1].body
        assert "새 업무로 분리하지 않고" in comments[-1].body
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_yuri_auto_route_persists_intake_context_for_short_followups(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    store = _TranscriptStore()
    runner = _runner()
    runner.session_store = store

    route = runner._classify_yuri_auto_route(
        _event("에르메스 기능중에 코덱스에 관련된 기능 찾아줘.")
    )
    response = await runner._route_yuri_message_to_kanban(
        _event("에르메스 기능중에 코덱스에 관련된 기능 찾아줘."),
        route,
    )

    assert response is not None
    assert "코덱스" in response
    assert [entry[1]["role"] for entry in store.entries] == ["user", "assistant"]
    assert "코덱스에 관련된 기능" in store.entries[0][1]["content"]
    assert "코덱스" in store.entries[1][1]["content"]
    assert store.updated == ["key-ctx"]


@pytest.mark.asyncio
async def test_yuri_delivery_followup_links_to_recent_done_task(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        parent_id = kb.create_task(
            conn,
            title="이미지 레이어 분리",
            assignee="planner",
            session_id="session-delivery",
        )
        kb.complete_task(conn, parent_id, summary="레이어 분리 산출물 준비")
    finally:
        conn.close()

    runner = _runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-delivery")
    )
    route = runner._classify_yuri_auto_route(_event("파일로 보내주세요"))
    response = await runner._route_yuri_message_to_kanban(
        _event("파일로 보내주세요"),
        route,
    )

    assert response is not None
    assert "문서팀에 이관하겠습니다" in response
    assert "원래 요청에 이어 결과만 보고하겠습니다" in response
    conn = kb.connect()
    try:
        children = kb.child_ids(conn, parent_id)
        assert len(children) == 1
        child = kb.get_task(conn, children[0])
        assert child.assignee == "docslead"
        assert "부모 업무" in (child.body or "")
        assert parent_id in (child.body or "")
        assert "실제 첨부가 불가능하면 완료 처리하지 말고 차단" in (child.body or "")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_yuri_context_dependent_request_does_not_fake_without_context(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    runner = _runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="empty-session")
    )
    route = runner._classify_yuri_auto_route(_event("파일로 보내주세요"))
    response = await runner._route_yuri_message_to_kanban(
        _event("파일로 보내주세요"),
        route,
    )

    assert "맥락을 먼저 확인해야 합니다" in response
    conn = kb.connect()
    try:
        assert kb.list_tasks(conn, session_id="empty-session") == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_yuri_pm_root_creates_single_visible_root_task(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    runner = _runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-2")
    )
    route = runner._classify_yuri_auto_route(
        _event(
            "63지선 방에 있는 스티커 이미지를 찾아 공개 접속 안내 레이어에 넣고 "
            "제작자 문구와 하얀 박스 크기까지 반영한 뒤 검수까지 진행해주세요",
            media=True,
        )
    )

    response = await runner._route_yuri_message_to_kanban(
        _event("복합 테스트", media=True),
        route,
    )

    assert response is not None
    assert "이관하겠습니다" in response
    assert "운영팀" in response
    conn = kb.connect()
    try:
        tasks = kb.list_tasks(conn, session_id="session-2")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.assignee == "planner"
        assert task.status == "ready"
        assert "하위 worker는 내부 코멘트/하위 카드로만" in (task.body or "")
        assert "63지선 텔레그램 방 미디어 조회가 1순위" in (task.body or "")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_yuri_contextual_followup_attaches_to_active_root(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        root_id = kb.create_task(
            conn,
            title="63지선 공개지도 안내 레이어 개선",
            body="유리 PM 오케스트레이션 루트 업무입니다.",
            assignee="planner",
            session_id="session-3",
        )
    finally:
        conn.close()

    runner = _runner()
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-3")
    )
    route = runner._classify_yuri_auto_route(_event("스티커도 넣고 하얀 박스도 키워도 좋아"))
    response = await runner._route_yuri_message_to_kanban(
        _event("스티커도 넣고 하얀 박스도 키워도 좋아"),
        route,
    )

    assert response is not None
    assert "이어 붙" in response
    conn = kb.connect()
    try:
        tasks = kb.list_tasks(conn, session_id="session-3")
        assert [t.id for t in tasks] == [root_id]
        comments = kb.list_comments(conn, root_id)
        assert comments
        assert "스티커도 넣고" in comments[-1].body
    finally:
        conn.close()


def test_yuri_auto_route_ignores_non_yuri_profiles_and_bots():
    assert _runner("researcher")._classify_yuri_auto_route(
        _event("최신 자료 조사해서 정리해주세요")
    ) is None
    assert _runner()._classify_yuri_auto_route(
        _event("최신 자료 조사해서 정리해주세요", is_bot=True)
    ) is None


def test_kanban_artifact_status_reports_missing_declared_file():
    runner = _runner()
    adapter = SimpleNamespace(extract_local_files=lambda text: ([], text))

    status = runner._kanban_artifact_status(
        adapter=adapter,
        event_payload={"artifacts": ["/tmp/hermes-missing-artifact.md"]},
        task=None,
    )

    assert status["declared"] == 1
    assert status["existing"] == 0
    assert status["missing"] == ["/tmp/hermes-missing-artifact.md"]


def test_kanban_file_delivery_claim_detector_requires_artifact_evidence():
    assert _kanban_text_claims_artifact_delivery("파일 전송 완료했습니다.") is True
    assert _kanban_text_claims_artifact_delivery("내용 정리 완료했습니다.") is False


def test_secretary_clean_kanban_text_summarizes_raw_failures():
    cleaned = _secretary_clean_kanban_text(
        "telegram",
        "⚠️ Cron job 'poll' failed: Script exited with code 255\nConnection closed by UNKNOWN port 65535",
    )

    assert "예약 작업에서 오류" in cleaned
    assert "Script exited" not in cleaned
    assert "UNKNOWN port" not in cleaned
