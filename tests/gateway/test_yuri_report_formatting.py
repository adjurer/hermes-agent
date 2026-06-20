from gateway.kanban_watchers import format_yuri_telegram_report_text


def test_yuri_report_formatter_expands_inline_bullets():
    text = (
        "대표님, 커뮤니티 커버리지 최신 확장 목록 기준으로 다시 정리했습니다. "
        "핵심 결론은 아래와 같습니다. - 현재 기준점은 미니피시 v3 baseline 92개 소스입니다. "
        "- 잇싸는 이미 확인된 핵심 P0 후보입니다. "
        "- Reddit, X/Twitter, Threads는 API gap이 있습니다. "
        "따라서 이번 확장안은 production 수집 완료가 아니라 smoke 목록입니다."
    )

    formatted = format_yuri_telegram_report_text(text)

    assert "같습니다. - 현재" not in formatted
    assert "\n- 현재 기준점은" in formatted
    assert "\n- 잇싸는" in formatted
    assert "\n- Reddit" in formatted
    assert "\n\n따라서 이번 확장안은" in formatted


def test_yuri_report_formatter_preserves_existing_paragraphs():
    text = "대표님, 확인했습니다.\n\n- 첫 번째\n- 두 번째\n\n결론입니다."

    formatted = format_yuri_telegram_report_text(text)

    assert formatted == text


def test_yuri_report_formatter_keeps_short_natural_reply_as_prose():
    text = "네, 지금 설정은 정상입니다. 다만 자동학습은 아니고 필요할 때 fact_store를 조회하는 구조입니다."

    formatted = format_yuri_telegram_report_text(text)

    assert formatted == text
    assert "\n" not in formatted


def test_yuri_report_formatter_does_not_turn_long_prose_into_sentence_checklist():
    text = (
        "결론은 현재 수집기는 운영 투입 전 검증 단계입니다. "
        "원천 파일은 확인됐지만 일부 샘플에서 readback 증거가 부족합니다. "
        "따라서 완료 보고보다는 검수 보류가 맞습니다. "
        "다음 단계는 누락 샘플을 다시 읽고 행수와 해시를 남기는 것입니다."
    )

    formatted = format_yuri_telegram_report_text(text)

    assert "- " not in formatted
    assert "\n\n" in formatted
    assert formatted.count("\n\n") <= 2
    assert "결론은 현재 수집기는 운영 투입 전 검증 단계입니다." in formatted
