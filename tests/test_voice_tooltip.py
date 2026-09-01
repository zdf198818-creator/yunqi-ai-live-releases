from ailive.client.app import MainWindow


def test_same_voice_name_and_text_ignores_suffix_and_end_punctuation() -> None:
    assert MainWindow._same_voice_name_and_text("大家好.wav", "大家好。")


def test_different_voice_name_and_text_are_not_hidden() -> None:
    assert not MainWindow._same_voice_name_and_text("科目一音色", "大家好")
