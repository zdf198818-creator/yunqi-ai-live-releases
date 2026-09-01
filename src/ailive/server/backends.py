from __future__ import annotations

import hashlib
import math
import os
from abc import ABC, abstractmethod

from ailive.domain import VoiceProfile


class TTSBackend(ABC):
    name = "abstract"

    @abstractmethod
    def synthesize(
        self, text: str, voice: VoiceProfile, language: str, randomness: str = "normal"
    ) -> tuple[object, int]:
        raise NotImplementedError


class MockTTSBackend(TTSBackend):
    """Deterministic tone generator used to exercise the pipeline without a GPU."""

    name = "mock"

    def synthesize(
        self, text: str, voice: VoiceProfile, language: str, randomness: str = "normal"
    ) -> tuple[object, int]:
        import numpy as np

        sample_rate = 24_000
        duration = min(max(len(text) * 0.08, 0.4), 5.0)
        frames = int(sample_rate * duration)
        frequency = 220 + (sum(voice.reference_id.encode("utf-8")) % 180)
        timeline = np.arange(frames, dtype=np.float32) / sample_rate
        envelope = np.minimum(1.0, np.minimum(timeline * 20, (duration - timeline) * 20))
        samples = 0.12 * np.sin(2 * math.pi * frequency * timeline) * envelope
        return samples.astype(np.float32), sample_rate


class QwenTTSBackend(TTSBackend):
    name = "qwen3-tts-1.7b-base"

    def __init__(self, model_path: str) -> None:
        import torch
        from qwen_tts import Qwen3TTSModel

        self.torch = torch
        torch.set_float32_matmul_precision("high")

        model_kwargs: dict[str, object] = {
            "device_map": "cuda:0",
            "dtype": torch.bfloat16,
        }
        try:
            import flash_attn  # noqa: F401

            model_kwargs["attn_implementation"] = "flash_attention_2"
        except ImportError:
            # Transformers otherwise falls back to the much slower eager
            # attention implementation for this model.  PyTorch SDPA uses
            # fused CUDA kernels on the RTX 4090 without an extra extension.
            model_kwargs["attn_implementation"] = "sdpa"

        self.model = Qwen3TTSModel.from_pretrained(model_path, **model_kwargs)
        if os.environ.get("AILIVE_USE_OPTIMIZED_QWEN", "0") == "1":
            enable_optimizations = getattr(
                self.model, "enable_streaming_optimizations", None
            )
            if enable_optimizations is None:
                raise RuntimeError("当前 qwen-tts 不包含快速解码优化实现")
            enable_optimizations(
                decode_window_frames=300,
                use_compile=True,
                use_cuda_graphs=False,
                compile_mode="reduce-overhead",
                use_fast_codebook=True,
                compile_codebook_predictor=True,
                compile_talker=True,
            )
        if os.environ.get("AILIVE_USE_TORCH_COMPILE", "0") == "1":
            compile_mode = os.environ.get("AILIVE_COMPILE_MODE", "reduce-overhead")
            # Match the proven optimized service layout: compile the main
            # autoregressive decoder and the per-frame code predictor while
            # leaving the audio tokenizer/codec outside torch.compile.
            talker = self.model.model.talker
            talker.model.forward = torch.compile(
                talker.model.forward,
                mode=compile_mode,
                fullgraph=False,
                dynamic=True,
            )
            talker.code_predictor.forward = torch.compile(
                talker.code_predictor.forward,
                mode=compile_mode,
                fullgraph=False,
                dynamic=True,
            )
        self.prompt_cache: dict[str, object] = {}

    def _prompt_for(self, voice: VoiceProfile) -> object:
        cached = self.prompt_cache.get(voice.reference_id)
        if cached is not None:
            return cached
        prompt = self.model.create_voice_clone_prompt(
            ref_audio=voice.audio_path,
            ref_text=voice.reference_text,
        )
        self.prompt_cache[voice.reference_id] = prompt
        return prompt

    def synthesize(
        self, text: str, voice: VoiceProfile, language: str, randomness: str = "normal"
    ) -> tuple[object, int]:
        prompt = self._prompt_for(voice)
        generation_options: dict[str, object] = {}
        if randomness == "low":
            generation_options = {
                "do_sample": True,
                "top_k": 20,
                "top_p": 0.85,
                "temperature": 0.65,
                "subtalker_dosample": True,
                "subtalker_top_k": 20,
                "subtalker_top_p": 0.85,
                "subtalker_temperature": 0.65,
            }
        elif randomness == "off":
            # Fully greedy decoding (both samplers set to False) is unsafe for
            # Qwen3-TTS: it can fail to emit EOS and occupy the GPU indefinitely,
            # leaving the client with an empty buffer.  Keep conservative
            # sampling enabled and seed it from stable request data instead.
            seed_material = "\0".join((voice.reference_id, language, text)).encode("utf-8")
            seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
            self.torch.manual_seed(seed)
            if self.torch.cuda.is_available():
                self.torch.cuda.manual_seed_all(seed)
            generation_options = {
                "do_sample": True,
                "top_k": 10,
                "top_p": 0.75,
                "temperature": 0.5,
                "repetition_penalty": 1.05,
                "subtalker_dosample": True,
                "subtalker_top_k": 10,
                "subtalker_top_p": 0.75,
                "subtalker_temperature": 0.5,
                "max_new_tokens": 1024,
            }
        wavs, sample_rate = self.model.generate_voice_clone(
            text=text,
            language=language,
            voice_clone_prompt=prompt,
            **generation_options,
        )
        return wavs[0], sample_rate


def build_backend(name: str, model_path: str) -> TTSBackend:
    if name == "mock":
        return MockTTSBackend()
    if name == "qwen":
        return QwenTTSBackend(model_path)
    raise ValueError(f"不支持的TTS后端: {name}")
