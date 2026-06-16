import json
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


def _runner(profile: str = "default") -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._active_profile_name = lambda: profile
    return runner


class _TranscriptStore:
    def __init__(self, session_id="session-ctx", history=None):
        self.session_id = session_id
        self.history = list(history or [])
        self.entries = []
        self.updated = []

    def get_or_create_session(self, source):
        return SimpleNamespace(session_id=self.session_id, session_key="key-ctx")

    def append_to_transcript(self, session_id, message, skip_db=False):
        self.entries.append((session_id, message))

    def load_transcript(self, session_id):
        return list(self.history)

    def update_session(self, session_key, last_prompt_tokens=None):
        self.updated.append(session_key)


def _event(
    text: str,
    *,
    media: bool = False,
    is_bot: bool = False,
    chat_type: str = "dm",
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
            chat_type=chat_type,
            is_bot=is_bot,
        ),
        message_id="m1",
        media_urls=["/tmp/example.png"] if media else [],
        media_types=["image/png"] if media else [],
    )


def test_yuri_raw_intake_records_exact_telegram_sentence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_RAW_INTAKE_DIR", str(tmp_path))
    runner = _runner()
    event = _event("내일 오전에 경기도 초선의원 비율 근거 다시 확인해야 해.")
    event.message_id = "raw-1"

    runner._record_yuri_raw_intake(event)
    runner._record_yuri_raw_intake(event)

    files = list((tmp_path / "telegram").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["text"] == "내일 오전에 경기도 초선의원 비율 근거 다시 확인해야 해."
    assert rows[0]["chat_id"] == "c1"
    assert rows[0]["message_id"] == "raw-1"


@pytest.mark.asyncio
async def test_yuri_kanban_intake_injects_knowledge_spine_context(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))
    kb.init_db()

    runner = _runner()
    event = _event("코드 오류 원인을 확인하고 수정해주세요.")
    event.message_id = "spine-msg-1"

    response = await runner._yuri_kanban_intake_reply(event, event.source)

    assert response == (
        "작업 큐에 접수했습니다. "
        "진행 상태가 궁금하시면 '지금 진행중인건가요?'라고 물어보시면 바로 확인하겠습니다. "
        "결과는 검수 후 보고드리겠습니다."
    )
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT id, body FROM tasks WHERE title LIKE '[YURI intake]%'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "YURI KNOWLEDGE SPINE CONTEXT PACK" in row["body"]
    assert "코드 오류 원인을 확인하고 수정해주세요." in row["body"]
    assert "accepted_intent_sources: telethon" in row["body"]

    events_path = tmp_path / "spine" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["kind"] == "yuri_intake"
    assert rows[-1]["payload"]["task_id"] == row["id"]


def test_yuri_recent_raw_intake_reply_returns_exact_prior_sentences(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_YURI_RAW_INTAKE_DIR", str(tmp_path))
    runner = _runner()
    first = _event("kg서버 휴먼 폴더 위치만 먼저 확인해줘.")
    first.message_id = "raw-1"
    second = _event("맥서버 Hermes 상태 증거도 따로 봐줘.")
    second.message_id = "raw-2"
    ask = _event("방금 내가 남긴 문장 원문 그대로 가져와줘.")
    ask.message_id = "raw-3"

    runner._record_yuri_raw_intake(first)
    runner._record_yuri_raw_intake(second)

    response = runner._yuri_recent_raw_intake_reply(ask)

    assert response is not None
    assert "최근 원문입니다." in response
    assert "맥서버 Hermes 상태 증거도 따로 봐줘." in response
    assert "kg서버 휴먼 폴더 위치만 먼저 확인해줘." in response


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("지금 살아있으면 OK라고만 답해줘", "literal_reply"),
        ("방금 내가 남긴 문장 원문 그대로 가져와줘", "recent_context"),
        ("방금 한 대답은 어떤 로직으로 답변한건가요?", "meta_explanation"),
        ("아니 휴먼 폴더 안에 어떤어떤 폴더들이 있는지 말한거에요", "correction"),
        ("kg서버 휴먼 폴더 루트만 알려줘", "path_lookup"),
        ("다시한번 휴먼 폴더 리스트 보여주세요", "content_lookup"),
        ("안녕?", "social_reply"),
        ("좋은 아침입니다", "social_reply"),
        ("고생 많았지?", "social_reply"),
        ("이제 별걸다 넘기네", "social_reply"),
        ("인사건 뭐건 모두 칸반으로 넘기고 있는것 같아요", "social_reply"),
        ("kg서버에 실제 파일이 있는지 확인해줘. 추측하지 말고 지금 접속해서 봐줘", "agent_turn"),
        ("kg서버의 비밀번호는 6501 입니다.", "agent_turn"),
        ("당선자 대수는 15·16·18·19·20·22대입니다 맞나요?", "agent_turn"),
        ("이 폴더 이름 바꾸고 옮겨주세요", "agent_turn"),
        ("인사말 문구 정리해주세요", "agent_turn"),
    ],
)
def test_yuri_telegram_intent_classifier_keeps_shortcuts_narrow(text, kind):
    assert _runner()._classify_yuri_telegram_intent(_event(text)).kind == kind


def test_yuri_review_gate_required_for_deliverables_and_completion_reports():
    runner = _runner()

    assert runner._yuri_review_gate_required("보고서 파일 만들어주세요", "보고서 생성 완료")
    assert runner._yuri_review_gate_required("대화를 보고 최적화 해주세요", "분석 결론입니다")
    assert runner._yuri_review_gate_required(
        "자료 정리",
        "정리했습니다",
        artifact_status={"declared": 1, "available": 1},
    )


def test_yuri_review_gate_exempts_exact_path_only_answers():
    assert not _runner()._yuri_review_gate_required(
        "kg서버 휴먼 폴더 루트만 알려줘",
        "kg서버 휴먼 폴더는 `/srv/poll-data/minjookg/human` 입니다.",
    )


def test_yuri_review_gate_pass_requires_intent_source_evidence():
    runner = _runner()

    assert runner._yuri_review_gate_passed(
        event_payload={"review_status": "pass", "intent_source": "telethon"}
    )
    assert runner._yuri_review_gate_passed(
        event_payload={"review_status": "pass", "intent_source": "telegram-safe"}
    )
    assert runner._yuri_review_gate_passed("검수 통과: 텔레쏜 확인 후 의도 일치")
    assert not runner._yuri_review_gate_passed("검수 통과")
    assert not runner._yuri_review_gate_passed("완료했습니다")
    assert runner._yuri_review_gate_passed(
        "review_status=pass, intent_source=telethon: 검수된 최종 문안입니다."
    )


@pytest.mark.parametrize(
    "text",
    [
        "지금 다시 한번 확인해줘",
        "대화방을 보고 원인을 파악해주세요",
        "아니 휴먼 폴더 안에 어떤 폴더들이 있는지 말한거에요",
        "유리가 왜 이렇게 답했는지 확인해주세요",
    ],
)
def test_yuri_actionable_telegram_work_routes_to_kanban_intake(text):
    assert _runner()._yuri_should_route_to_kanban_intake(_event(text))


@pytest.mark.parametrize(
    "text",
    [
        "지금 살아있으면 OK라고만 답해줘",
        "kg서버 휴먼 폴더 루트만 알려줘",
        "내가 원하는 보고 방식 한 문장으로 말해줘",
        "지금 진행중인건가요?",
        "수집기가 돌고있나요?",
        "칸반 상태가 어떤가요?",
        "네 좋아요",
        "응",
        "고마워",
        "안녕?",
        "좋은 아침입니다",
        "고생 많았지?",
        "이제 별걸다 넘기네",
        "그냥 인사한거야",
        "인사건 뭐건 모두 칸반으로 넘기고 있는것 같아요",
    ],
)
def test_yuri_tiny_or_exact_answers_do_not_route_to_kanban_intake(text):
    assert not _runner()._yuri_should_route_to_kanban_intake(_event(text))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("안녕?", "안녕하세요"),
        ("좋은 아침입니다", "안녕하세요"),
        ("고생 많았지?", "오늘도 바로 보겠습니다"),
        ("이제 별걸다 넘기네", "바로 답해야 합니다"),
        ("인사건 뭐건 모두 칸반으로 넘기고 있는것 같아요", "바로 답해야 합니다"),
    ],
)
async def test_yuri_social_turns_reply_directly_without_kanban_ack(text, expected):
    runner = _runner()

    response = await runner._yuri_fast_lookup_reply(_event(text))

    assert response is not None
    assert expected in response
    assert "결과는 검수 후" not in response
    assert "배정" not in response
    assert not runner._yuri_should_route_to_kanban_intake(_event(text))


@pytest.mark.parametrize(
    "text",
    [
        "답변 첫머리에 TID=RND-01 붙이고, 아래 요청을 받으면 유리가 바로 해야 할 첫 행동만 말해줘. 발화: 중앙당 비공표 조사일 겁니다.",
        "답변 첫머리에 TID=RND-02 붙이고, 아래 내용에서 대표님께 확인해야 할 핵심 포인트 1개만 뽑아줘. 내용: 구리,부천,파주 재심 없나요?",
        "답변 첫머리에 TID=RND-03 붙이고, 아래 텔레쏜 내용을 보고 놓치면 안 되는 리스크가 있으면 한 줄로 말해줘. 내용: 예. 알고 있습니다",
        "답변 첫머리에 TID=RND-04 붙이고, 아래 메시지에 대해 유리답게 짧고 자연스럽게 응답해줘. 메시지: 2번 같은경우 본선 후보자 기준입니다.",
        "답변 첫머리에 TID=RND-05 붙이고, 아래 텔레쏜 발화가 업무 지시인지 단순 정보인지 한 줄로 판단해줘. 발화: 대통령 해외 순방 중 사상",
    ],
)
@pytest.mark.asyncio
async def test_yuri_tid_brief_direct_prompts_reply_without_kanban_intake(text):
    runner = _runner()

    response = await runner._yuri_fast_lookup_reply(_event(text))

    assert response is not None
    assert response.startswith("TID=RND-")
    assert "넘겨" not in response
    assert "배정" not in response
    assert not runner._yuri_should_route_to_kanban_intake(_event(text))


@pytest.mark.asyncio
async def test_yuri_kanban_intake_ack_preserves_tid_when_work_must_route(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))
    kb.init_db()

    runner = _runner()
    response = await runner._yuri_kanban_intake_reply(
        _event("TID=WORK-A1 코드 오류 원인을 확인하고 수정해주세요."),
        _event("x").source,
    )

    assert response.startswith("TID=WORK-A1 ")
    assert "작업 큐에 접수했습니다" in response
    assert "지금 진행중인건가요?" in response
    assert "결과는 검수 후" in response


def test_yuri_work_type_prompt_with_payload_summary_word_routes_to_kanban():
    text = (
        "TID=WRK50-47 업무형 50건 검증입니다. 아래 텔레쏜 메모를 업무 메모로 보고 "
        "문제점 1개와 바로 쓸 수정안 1개를 작성해주세요. 최종 보고 첫머리는 반드시 "
        "TID=WRK50-47 로 시작해야 합니다. 메모: 코인니스가 요약 정리해 송고한다."
    )
    runner = _runner()

    assert runner._yuri_brief_direct_reply(_event(text)) is None
    assert runner._yuri_should_route_to_kanban_intake(_event(text))


def test_yuri_handoff_mismatch_does_not_treat_national_event_name_as_scope_error():
    runner = _runner()

    assert runner._yuri_handoff_mismatch_warning(
        "TID=WRK50B-40 지방의원전국대회 참석 안내를 경기도 광역·기초의원에게 보내야 합니다.",
        "TID=WRK50B-40 경기도 광역·기초의원 당선자 전체 명단 기준으로 안내합니다.",
    ) is None


def test_yuri_handoff_mismatch_preserves_tid_for_real_scope_error():
    warning = _runner()._yuri_handoff_mismatch_warning(
        "TID=SCOPE-1 전국 전체 지방선거 자료를 정리해주세요.",
        "경기도 기준으로 정리했습니다.",
    )

    assert warning is not None
    assert warning.startswith("TID=SCOPE-1 ")


@pytest.mark.asyncio
async def test_yuri_fast_lookup_human_folder_root_accepts_root_word():
    response = await _runner()._yuri_fast_lookup_reply(
        _event("kg서버 휴먼 폴더 루트만 알려줘")
    )

    assert response == "kg서버 휴먼 폴더는 `/srv/poll-data/minjookg/human` 입니다."


@pytest.mark.asyncio
async def test_yuri_fast_lookup_clarifies_ambiguous_ordinal_local_election_question():
    response = await _runner()._yuri_fast_lookup_reply(
        _event("22대 지선에서 초선의 당선은 몇퍼센트입니까?")
    )

    assert response is not None
    assert "질문 기준이 모호합니다" in response
    assert "국회/총선" in response
    assert "지방선거" in response


@pytest.mark.parametrize(
    "text",
    [
        "[50회검증 01] kg서버 휴먼 폴더 루트만 알려줘",
        "[50회검증 02] kg서버 human 폴더 경로만 알려줘",
        "[50회검증 03] 휴먼 폴더 위치만 한 줄로 알려줘",
        "[50회검증 04] kg human folder root 알려줘",
        "[50회검증 05] kg서버 사람 폴더 경로만 알려줘",
        "[50회검증 06] kg서버 휴먼 폴더 어디야",
        "[50회검증 07] kg서버 human 위치만 알려줘",
        "[50회검증 08] 휴먼 폴더 루트 알려줘",
        "[50회검증 09] kg서버 휴먼 경로 알려줘",
        "[50회검증 10] kg서버 사람 폴더 위치 알려줘",
        "[50회검증 11] human 폴더 루트만 알려줘",
        "[50회검증 12] 휴먼 folder root만 알려줘",
        "[50회검증 13] kg서버 휴먼 폴더 절대경로 알려줘",
        "[50회검증 14] kg서버 휴먼 폴더 위치만 답해줘",
        "[50회검증 15] kg human 경로만 답해줘",
        "[50회검증 16] kg서버 휴먼 폴더 경로 한 줄",
        "[50회검증 17] kg서버 minjookg human 폴더 위치 알려줘",
        "[50회검증 18] 휴먼 폴더 경로를 한 줄로만 알려줘",
        "[50회검증 19] kg서버 휴먼 루트만 알려줘",
        "[50회검증 20] kg서버 human root만 말해줘",
    ],
)
@pytest.mark.asyncio
async def test_yuri_fast_lookup_human_folder_root_accepts_live_variants(text):
    response = await _runner()._yuri_fast_lookup_reply(_event(text))

    assert response == "kg서버 휴먼 폴더는 `/srv/poll-data/minjookg/human` 입니다."


@pytest.mark.parametrize(
    "text",
    [
        "휴먼 폴더 안에 어떤어떤 폴더들이 있는지 말해줘",
        "다시한번 휴먼 폴더 리스트 보여주세요",
        "kg서버 휴먼 폴더 안에 뭐뭐가 있어?",
        "휴먼 폴더 내용물 파악해봐",
    ],
)
@pytest.mark.asyncio
async def test_yuri_fast_lookup_does_not_replace_human_folder_contents_with_path(text):
    response = await _runner()._yuri_fast_lookup_reply(_event(text))

    assert response is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("내가 선호하는 보고 방식 한 문장으로 말해줘", "검증 결과"),
        ("내가 싫어하는 내부 처리 멘트 예시 하나만 말해줘", "내부 처리 멘트"),
        ("내가 기억 리셋을 말한 이유를 한 줄로 요약해줘", "오염된 기억"),
        ("내가 원하는 24시간 비서의 핵심 역할 한 줄", "24시간 비서"),
    ],
)
@pytest.mark.asyncio
async def test_yuri_fast_lookup_personal_operating_preferences(text, expected):
    response = await _runner()._yuri_fast_lookup_reply(_event(f"[혼합50] {text}"))

    assert response is not None
    assert expected in response


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("미니서버 상태 질문이면 kg서버와 섞지 말고 어떻게 봐야 해? 한 줄", "현재 접속"),
        ("미니피시와 kg서버를 헷갈리면 안 되는 이유 한 줄", "별도 장비"),
        ("미니서버 관련 파일 위치 질문은 추측해도 돼? 한 줄", "아니요"),
        ("미니서버 작업 완료 보고에는 어떤 증거가 필요해? 한 줄", "로그"),
    ],
)
@pytest.mark.asyncio
async def test_yuri_fast_lookup_mini_server_operating_rules(text, expected):
    response = await _runner()._yuri_fast_lookup_reply(_event(f"[혼합50] {text}"))

    assert response is not None
    assert expected in response


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("kg서버 상태 질문이면 어떤 서버 기준으로 봐야 해? 한 줄", "Linux KG 서버"),
        ("kg서버 관련 질문에서 예전 기억이 섞이면 어떻게 해야 해? 한 줄", "현재 접속"),
        ("kg서버 상태 확인은 예전 설정이 아니라 뭘 봐야 해?", "현재 접속"),
        ("kg서버 작업은 Windows 대시보드랑 섞어도 돼? 한 줄", "아니요"),
        ("kg서버 작업 완료 기준은 추측이야 실제 검증이야? 한 단어로", "실제 검증"),
        ("kg서버에 실제 파일이 있는지 확인해줘. 추측하지 말라는 원칙만 말해줘", "추측하지"),
    ],
)
@pytest.mark.asyncio
async def test_yuri_fast_lookup_kg_server_operating_rules(text, expected):
    response = await _runner()._yuri_fast_lookup_reply(_event(f"[혼합50] {text}"))

    assert response is not None
    assert expected in response


@pytest.mark.asyncio
async def test_yuri_fast_lookup_does_not_replace_kg_progress_questions_with_generic_rule():
    response = await _runner()._yuri_fast_lookup_reply(
        _event("kg서버 기준 우리가 하려는건 얼마나 된거같아?")
    )

    assert response is None


@pytest.mark.asyncio
async def test_yuri_progress_status_question_returns_status_without_new_intake():
    runner = _runner()
    runner._yuri_current_work_status = lambda: "네, 현재 작업 상태는 진행 중 1건입니다."

    response = await runner._yuri_fast_lookup_reply(_event("지금 진행중인건가요?"))

    assert response == "네, 현재 작업 상태는 진행 중 1건입니다."
    assert not runner._yuri_should_route_to_kanban_intake(_event("지금 진행중인건가요?"))


@pytest.mark.asyncio
async def test_yuri_fast_lookup_human_path_wins_when_mini_is_negated():
    response = await _runner()._yuri_fast_lookup_reply(
        _event("KG가 아닌 미니서버 얘기는 하지 말고 human 경로만")
    )

    assert response is not None
    assert "/srv/poll-data/minjookg/human" in response
    assert "미니서버" not in response


@pytest.mark.asyncio
async def test_yuri_fast_lookup_mixed_kg_human_and_mini_returns_both():
    response = await _runner()._yuri_fast_lookup_reply(
        _event("KG human 경로와 미니서버 확인 기준을 각각 한 줄로, 서로 섞지 않았다고 표시해줘")
    )

    assert response is not None
    assert "/srv/poll-data/minjookg/human" in response
    assert "미니서버" in response


def test_yuri_literal_only_reply_ok_avoids_model_and_routing():
    response = _runner()._yuri_literal_only_reply(
        _event("[50회검증 21] 진단입니다. OK라고만 답해줘")
    )

    assert response == "OK"


def test_yuri_literal_only_reply_preserves_requested_tid():
    response = _runner()._yuri_literal_only_reply(
        _event("[TID=GEN-A1] 진단입니다. 답변 첫머리에 TID=GEN-A1 붙이고 OK라고만 답해줘")
    )

    assert response == "TID=GEN-A1 OK"


@pytest.mark.asyncio
async def test_yuri_fast_lookup_preserves_requested_tid():
    response = await _runner()._yuri_fast_lookup_reply(
        _event("[TID=KG-A1] 답변 첫머리에 TID=KG-A1 붙이고 kg서버 human 경로만 말해줘")
    )

    assert response.startswith("TID=KG-A1 ")
    assert "/srv/poll-data/minjookg/human" in response


def test_yuri_liveness_honors_ok_only_request():
    response = _runner()._quick_liveness_response(
        _event("지금 살아있으면 OK라고만 답해줘"),
        "agent:main:telegram:dm:c1",
    )

    assert response == "OK"


def test_yuri_auto_route_layer_is_removed():
    runner = _runner()

    assert not hasattr(runner, "_classify_yuri_auto_route")
    assert not hasattr(runner, "_route_yuri_message_to_kanban")


def test_yuri_protocol_prompt_is_injected_for_telegram_default_profile():
    runner = _runner()
    prompt = runner._yuri_secretary_protocol_prompt(_event("방금 오류 뭐야?"), _event("x").source)

    assert "YURI secretary protocol" in prompt
    assert "와다다다" in prompt
    assert "말하지 않아도" in prompt
    assert "기획실/planner 루트 Kanban 카드" in prompt
    assert "마무리했습니다" in prompt
    assert "페이커, 올리비아" in prompt
    assert "명시적으로 대량/장기 분배" not in prompt


def test_yuri_protocol_prompt_ignores_worker_profiles():
    runner = _runner("researcher")

    assert runner._yuri_secretary_protocol_prompt(_event("방금 오류 뭐야?"), _event("x").source) == ""


def test_yuri_completion_guard_blocks_wrong_host_result():
    msg = GatewayRunner._yuri_handoff_mismatch_warning(
        "네 진행해주세요. 맥이 아니라 kg서버를 이용해주세요. 지방선거 민주당 당선자 초선 비율 분석",
        "Windows 대시보드 서버의 C:\\Users\\user\\poll-normalization에서 8787 대시보드 복구를 완료했습니다.",
    )

    assert "완료 보고를 보류했습니다" in msg
    assert "Windows 8787 대시보드" in msg


def test_yuri_completion_guard_blocks_forbidden_application():
    msg = GatewayRunner._yuri_handoff_mismatch_warning(
        "상황판에는 반영하지 말아줘. html 시안만 봐줘.",
        "상황판에 반영했습니다. 구현 완료 및 검증 완료.",
    )

    assert "완료 보고를 보류했습니다" in msg
    assert "금지 조건" in msg


def test_yuri_completion_guard_blocks_server_retention_delivery_mismatch():
    msg = GatewayRunner._yuri_handoff_mismatch_warning(
        "인수인계서는 나한테 보내지말고 서버에 보관해줘.",
        "파일을 첨부했습니다. 전송했습니다.",
    )

    assert "완료 보고를 보류했습니다" in msg
    assert "서버/사람 폴더 보관 기준" in msg


def test_yuri_completion_guard_blocks_narrowed_full_election_scope():
    msg = GatewayRunner._yuri_handoff_mismatch_warning(
        "기초단체장 뿐만 아니라 모든 지방선거에서의 비율을 알고싶어.",
        "경기도 기초단체장 민주당 초선 비율을 정리했습니다.",
    )

    assert "완료 보고를 보류했습니다" in msg
    assert "지방선거 전체 범위" in msg


def test_yuri_completion_guard_blocks_jiseon_to_general_election_recast():
    msg = GatewayRunner._yuri_handoff_mismatch_warning(
        "22대 지선에서 초선의 당선은 몇퍼센트입니까?",
        "22대 국회 초선 의원은 132명/300명으로 44.0%입니다.",
    )

    assert "완료 보고를 보류했습니다" in msg
    assert "총선/국회" in msg


def test_yuri_completion_guard_blocks_nationwide_scope_narrowed_to_gyeonggi():
    msg = GatewayRunner._yuri_handoff_mismatch_warning(
        "대부분 맞지만 경기도 한정이 아니야, 전국이야. 전국 정치·선거 통합 체계 기준으로 정리해줘.",
        "경기도 정치·선거 통합 작업장 기준으로 정리했습니다.",
    )

    assert "완료 보고를 보류했습니다" in msg
    assert "전국 범위" in msg
    assert "경기도 기준" in msg


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
