import unittest

from ailive.domain import ScriptLine


class ScriptLineTests(unittest.TestCase):
    def test_accepts_per_line_speed(self) -> None:
        line = ScriptLine("line-1", "voice-1", "测试#500#继续", speed=1.15)
        self.assertEqual(line.speed, 1.15)

    def test_rejects_out_of_range_speed(self) -> None:
        with self.assertRaises(ValueError):
            ScriptLine("line-1", "voice-1", "测试", speed=2.1)

    def test_randomness_round_trip(self) -> None:
        line = ScriptLine("line-1", "voice-1", "测试", randomness="low")
        self.assertEqual(ScriptLine.from_dict(line.to_dict()).randomness, "low")

    def test_rejects_unknown_randomness(self) -> None:
        with self.assertRaises(ValueError):
            ScriptLine("line-1", "voice-1", "测试", randomness="unknown")


if __name__ == "__main__":
    unittest.main()
