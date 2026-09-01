from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    backend: str
    model_path: str
    data_dir: Path
    api_token: str
    ffmpeg: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            backend=os.getenv("AILIVE_BACKEND", "mock").lower(),
            model_path=os.getenv("AILIVE_MODEL_PATH", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),
            data_dir=Path(os.getenv("AILIVE_DATA_DIR", "./data")).resolve(),
            api_token=os.getenv("AILIVE_API_TOKEN", "change-me"),
            ffmpeg=os.getenv("AILIVE_FFMPEG", "ffmpeg"),
        )
