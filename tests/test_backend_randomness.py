from __future__ import annotations

from ailive.domain import VoiceProfile
from ailive.server.backends import QwenTTSBackend


class FakeModel:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def generate_voice_clone(self, **kwargs: object) -> tuple[list[list[float]], int]:
        self.kwargs = kwargs
        return [[0.0]], 24_000


class FakeCuda:
    def __init__(self) -> None:
        self.seeds: list[int] = []

    def is_available(self) -> bool:
        return True

    def manual_seed_all(self, seed: int) -> None:
        self.seeds.append(seed)


class FakeTorch:
    def __init__(self) -> None:
        self.seeds: list[int] = []
        self.cuda = FakeCuda()

    def manual_seed(self, seed: int) -> None:
        self.seeds.append(seed)


def backend_with_fake_model() -> tuple[QwenTTSBackend, FakeModel, FakeTorch, VoiceProfile]:
    backend = object.__new__(QwenTTSBackend)
    model = FakeModel()
    fake_torch = FakeTorch()
    voice = VoiceProfile("voice-1", "测试", "reference.wav", "参考文案")
    backend.model = model
    backend.torch = fake_torch
    backend.prompt_cache = {"voice-1": "prompt"}
    return backend, model, fake_torch, voice


def test_low_randomness_uses_conservative_sampling() -> None:
    backend, model, _fake_torch, voice = backend_with_fake_model()
    backend.synthesize("测试", voice, "Chinese", "low")

    assert model.kwargs["do_sample"] is True
    assert model.kwargs["temperature"] == 0.65
    assert model.kwargs["subtalker_temperature"] == 0.65


def test_off_randomness_uses_seeded_sampling_instead_of_greedy_decoding() -> None:
    backend, model, fake_torch, voice = backend_with_fake_model()
    backend.synthesize("测试", voice, "Chinese", "off")

    assert model.kwargs["do_sample"] is True
    assert model.kwargs["subtalker_dosample"] is True
    assert model.kwargs["temperature"] == 0.5
    assert model.kwargs["subtalker_temperature"] == 0.5
    assert model.kwargs["max_new_tokens"] == 1024
    assert len(fake_torch.seeds) == 1
    assert fake_torch.cuda.seeds == fake_torch.seeds


def test_off_randomness_seed_is_stable_for_the_same_request() -> None:
    backend, _model, fake_torch, voice = backend_with_fake_model()

    backend.synthesize("同一句话", voice, "Chinese", "off")
    backend.synthesize("同一句话", voice, "Chinese", "off")

    assert len(fake_torch.seeds) == 2
    assert fake_torch.seeds[0] == fake_torch.seeds[1]


def test_off_randomness_seed_changes_with_the_text() -> None:
    backend, _model, fake_torch, voice = backend_with_fake_model()

    backend.synthesize("第一句话", voice, "Chinese", "off")
    backend.synthesize("第二句话", voice, "Chinese", "off")

    assert fake_torch.seeds[0] != fake_torch.seeds[1]
