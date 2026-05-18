from gateway.run import GatewayRunner


def test_natural_model_switch_accepts_main_model_phrases():
    expected = "/model gpt-5.5 --provider openai-codex"

    for text in (
        "GPT로 모델 변경해줘",
        "gpt-5.5로 모델 변경해줘",
        "gpt5.5로 변경",
        "코덱스로 복귀",
        "기본모델로 돌아와",
        "모델을 gpt로 변경해줘",
        "모델 다시 gpt-5.5로 변경해줘",
    ):
        assert GatewayRunner._natural_model_switch_command(text) == expected


def test_natural_model_switch_does_not_accept_removed_local_model():
    assert GatewayRunner._natural_model_switch_command("슈퍼젬마로 모델 변경해줘") == ""
