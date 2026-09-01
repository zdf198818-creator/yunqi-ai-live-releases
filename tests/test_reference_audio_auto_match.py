from ailive.client.app import MainWindow


def test_reference_match_ignores_pause_markers_and_spaces() -> None:
    script = "来，所有同学 #500# 进入直播间。"
    reference = "来，所有同学进入直播间"
    assert MainWindow._reference_match_text(script) == MainWindow._reference_match_text(reference)


def test_reference_match_keeps_different_wording_distinct() -> None:
    assert MainWindow._reference_match_text("第一句话。") != MainWindow._reference_match_text("第二句话。")


def test_reference_match_ignores_punctuation() -> None:
    assert MainWindow._reference_match_text("来，同学们！") == MainWindow._reference_match_text("来同学们")


def test_reference_match_expands_random_name_choices() -> None:
    variants = MainWindow._reference_match_variants(
        "[陈新忠|快乐|幸福]选择A，[思安|多年|万博]选择A。"
    )
    assert MainWindow._reference_match_text("幸福选择A，万博选择A") in variants


def test_best_reference_match_accepts_ninety_percent_similarity() -> None:
    variants = {"来所有同学进入直播间开始今天课程"}
    references = [("来所有同学进入直播间开始今日课程", "voice-1")]
    assert MainWindow._best_reference_match(variants, references) == "voice-1"


def test_best_reference_match_rejects_unrelated_audio() -> None:
    variants = {"这是一道交通标志题"}
    references = [("欢迎大家进入直播间学习课程", "wrong")]
    assert MainWindow._best_reference_match(variants, references) is None
