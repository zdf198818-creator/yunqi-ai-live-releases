import io
import unittest
import wave

import numpy as np
from PySide6.QtCore import QCoreApplication
from PySide6.QtMultimedia import QAudioFormat
from PySide6.QtTest import QTest

from ailive.client.audio import (
    AudioLine,
    AudioQueuePlayer,
    AudioToken,
    convert_pcm16_wav,
    decode_pcm16_wav,
    formats_match,
    render_line_tokens,
)
from ailive.server.audio import change_speed, pcm16_wav


class ClientAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def test_decodes_server_pcm16_wav(self) -> None:
        wav_bytes = pcm16_wav(np.zeros(2400, dtype=np.float32), 24000)
        pcm, audio_format = decode_pcm16_wav(wav_bytes)
        self.assertEqual(len(pcm), 4800)
        self.assertEqual(audio_format.sampleRate(), 24000)
        self.assertEqual(audio_format.channelCount(), 1)

    def test_mock_speed_fallback_shortens_audio(self) -> None:
        wav_bytes = pcm16_wav(np.zeros(24000, dtype=np.float32), 24000)
        faster = change_speed(
            wav_bytes,
            speed=2.0,
            ffmpeg="definitely-not-installed",
            allow_mock_fallback=True,
        )
        with wave.open(io.BytesIO(faster), "rb") as source:
            self.assertEqual(source.getnframes(), 12000)

    def test_converts_24khz_mono_to_48khz_stereo(self) -> None:
        wav_bytes = pcm16_wav(np.zeros(2400, dtype=np.float32), 24000)
        target = QAudioFormat()
        target.setSampleRate(48000)
        target.setChannelCount(2)
        target.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        pcm = convert_pcm16_wav(wav_bytes, target)

        self.assertEqual(len(pcm), 4800 * 2 * 2)

    def test_renders_middle_pause_inside_one_continuous_pcm_stream(self) -> None:
        wav_bytes = pcm16_wav(np.zeros(2400, dtype=np.float32), 24000)
        target = QAudioFormat()
        target.setSampleRate(24000)
        target.setChannelCount(1)
        target.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        pcm = render_line_tokens(
            [
                AudioToken(kind="audio", wav_bytes=wav_bytes),
                AudioToken(kind="pause", duration_ms=500),
                AudioToken(kind="audio", wav_bytes=wav_bytes),
            ],
            target,
        )

        self.assertEqual(len(pcm), (2400 + 12000 + 2400) * 2)

    def test_audio_formats_match_for_reusable_output_sink(self) -> None:
        first = QAudioFormat()
        first.setSampleRate(48000)
        first.setChannelCount(2)
        first.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        same = QAudioFormat(first)
        different = QAudioFormat(first)
        different.setSampleRate(24000)

        self.assertTrue(formats_match(first, same))
        self.assertFalse(formats_match(first, different))

    def test_sentence_end_pause_waits_for_the_line_pause_token(self) -> None:
        player = AudioQueuePlayer()
        paused: list[bool] = []
        player.sentenceEndPaused.connect(lambda: paused.append(True))
        player.enqueue(
            AudioLine(
                line_id="line-1",
                tokens=[AudioToken(kind="pause", duration_ms=20)],
            )
        )

        player.start()
        player.request_pause_after_current_line()
        self.assertFalse(player.is_paused)
        QTest.qWait(35)

        self.assertTrue(player.is_paused)
        self.assertEqual(paused, [True])

    def test_middle_pauses_continue_until_the_line_finishes(self) -> None:
        player = AudioQueuePlayer()
        finished: list[str] = []
        player.lineFinished.connect(finished.append)
        player.enqueue(
            AudioLine(
                line_id="line-1",
                tokens=[
                    AudioToken(kind="pause", duration_ms=10),
                    AudioToken(kind="pause", duration_ms=10),
                ],
            )
        )

        player.start()
        QTest.qWait(35)

        self.assertEqual(finished, ["line-1"])


if __name__ == "__main__":
    unittest.main()
