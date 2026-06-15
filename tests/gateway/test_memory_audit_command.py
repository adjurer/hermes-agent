import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="c1",
            user_id="u1",
            user_name="tester",
            chat_type="dm",
        ),
        message_id="m1",
    )


@pytest.mark.asyncio
async def test_memory_audit_command_returns_yuri_spine_report(tmp_path, monkeypatch):
    from gateway import yuri_knowledge_spine as spine

    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))

    pack = spine.build_context_pack(
        original_user_text="텔레쏜 대화 기준으로 유리 원인을 확인해주세요.",
        platform="telegram",
    )
    spine.record_intake(pack, task_id="t_audit")

    runner = object.__new__(GatewayRunner)
    out = await runner._handle_memory_command(
        _event("/memory audit 유리 원인 확인")
    )

    assert "Yuri memory audit" in out
    assert "relevant_spine_events" in out
    assert "t_audit" in out


@pytest.mark.asyncio
async def test_memory_graph_export_command_returns_yuri_graph_report(tmp_path, monkeypatch):
    from gateway import yuri_knowledge_spine as spine

    monkeypatch.setenv("HERMES_YURI_KNOWLEDGE_SPINE_DIR", str(tmp_path / "spine"))

    pack = spine.build_context_pack(
        original_user_text="텔레쏜 대화 기준으로 유리 원인을 확인해주세요.",
        platform="telegram",
    )
    spine.record_intake(pack, task_id="t_graph")

    runner = object.__new__(GatewayRunner)
    out = await runner._handle_memory_command(
        _event("/memory graph-export 유리 원인 확인")
    )

    assert "Yuri graph export" in out
    assert "HAS_USER_INTENT" in out
    assert "task:t_graph" in out
