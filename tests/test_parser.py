import unittest

from ailive.parser import (
    ScriptToken,
    parse_script,
    resolve_random_choices,
    split_script_sentences,
)


class ParseScriptTests(unittest.TestCase):
    def test_splits_speech_and_millisecond_pauses(self) -> None:
        self.assertEqual(
            parse_script("第一段#500#第二段#1000#"),
            [
                ScriptToken(kind="speech", text="第一段"),
                ScriptToken(kind="pause", duration_ms=500),
                ScriptToken(kind="speech", text="第二段"),
                ScriptToken(kind="pause", duration_ms=1000),
            ],
        )

    def test_does_not_scale_or_merge_pauses(self) -> None:
        self.assertEqual(
            parse_script("开始#300##500#继续"),
            [
                ScriptToken(kind="speech", text="开始"),
                ScriptToken(kind="pause", duration_ms=300),
                ScriptToken(kind="pause", duration_ms=500),
                ScriptToken(kind="speech", text="继续"),
            ],
        )

    def test_rejects_extreme_pause(self) -> None:
        with self.assertRaises(ValueError):
            parse_script("开始#60001#继续")

    def test_splits_document_into_one_sentence_per_row(self) -> None:
        self.assertEqual(
            split_script_sentences("第一句。第二句#500#继续！\n第三行没有句号"),
            ["第一句。", "第二句#500#继续！", "第三行没有句号"],
        )

    def test_resolves_one_option_from_each_random_group(self) -> None:
        resolved = resolve_random_choices(
            "[陈新忠|快乐|幸福]选择A，[思安|多年|万博]选择B"
        )
        first_name, second_name = resolved.split("选择A，")
        second_name = second_name.removesuffix("选择B")
        self.assertIn(first_name, {"陈新忠", "快乐", "幸福"})
        self.assertIn(second_name, {"思安", "多年", "万博"})
        self.assertNotIn("[", resolved)
        self.assertNotIn("|", resolved)

    def test_random_group_is_resolved_before_pause_parsing(self) -> None:
        tokens = parse_script("[甲|乙]回答#500#继续")
        self.assertIn(tokens[0].text, {"甲回答", "乙回答"})
        self.assertEqual(tokens[1], ScriptToken(kind="pause", duration_ms=500))

    def test_supports_chinese_brackets_and_fullwidth_pipe(self) -> None:
        resolved = resolve_random_choices("【甲｜乙｜丙】回答")
        self.assertIn(resolved, {"甲回答", "乙回答", "丙回答"})


if __name__ == "__main__":
    unittest.main()
