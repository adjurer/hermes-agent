from hermes_cli import yuri_quality as yq


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
