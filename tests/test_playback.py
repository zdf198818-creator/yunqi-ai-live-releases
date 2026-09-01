import unittest

from ailive.playback import PlaybackController, PlaybackState


class PlaybackControllerTests(unittest.TestCase):
    def test_sentence_end_pause_finishes_current_line_then_pauses(self) -> None:
        controller = PlaybackController()
        controller.start(total_lines=3)
        controller.on_buffer_ready()
        controller.request_sentence_end_pause()

        self.assertEqual(controller.state, PlaybackState.PAUSE_PENDING)
        self.assertEqual(controller.current_index, 0)

        controller.on_line_complete()
        self.assertEqual(controller.state, PlaybackState.PAUSED)
        self.assertEqual(controller.current_index, 1)

        controller.resume()
        self.assertEqual(controller.state, PlaybackState.PLAYING)

    def test_last_line_completes_instead_of_pausing(self) -> None:
        controller = PlaybackController()
        controller.start(total_lines=1)
        controller.on_buffer_ready()
        controller.request_sentence_end_pause()
        controller.on_line_complete()
        self.assertEqual(controller.state, PlaybackState.COMPLETED)

    def test_second_pause_click_cancels_pending_pause(self) -> None:
        controller = PlaybackController()
        controller.start(total_lines=2)
        controller.on_buffer_ready()
        controller.request_sentence_end_pause()
        controller.request_sentence_end_pause()
        self.assertEqual(controller.state, PlaybackState.PLAYING)


if __name__ == "__main__":
    unittest.main()
