import tempfile
import unittest
from pathlib import Path

from ailive.server.repository import VoiceRepository


class VoiceRepositoryTests(unittest.TestCase):
    def test_creates_and_reads_voice_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = VoiceRepository(Path(directory))
            created = repository.create(
                name="测试音色",
                reference_text="这是准确的参考文字。",
                suffix=".wav",
                content=b"RIFF-test",
            )

            loaded = repository.get(created.reference_id)
            self.assertEqual(loaded, created)
            self.assertEqual(repository.list_all(), [created])
            self.assertTrue(Path(created.audio_path).exists())


if __name__ == "__main__":
    unittest.main()
