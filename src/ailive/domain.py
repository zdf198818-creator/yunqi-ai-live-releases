from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    reference_id: str
    name: str
    audio_path: str
    reference_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScriptLine:
    line_id: str
    reference_id: str
    text: str
    speed: float = 1.0
    language: str = "Chinese"
    randomness: str = "normal"

    def __post_init__(self) -> None:
        if not self.line_id.strip():
            raise ValueError("line_id不能为空")
        if not self.reference_id.strip():
            raise ValueError("reference_id不能为空")
        if not self.text.strip():
            raise ValueError("话术不能为空")
        if not 0.5 <= self.speed <= 2.0:
            raise ValueError("语速必须在0.5到2.0之间")
        if self.randomness not in {"normal", "low", "off"}:
            raise ValueError("随机性模式必须是normal、low或off")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScriptLine:
        return cls(
            line_id=str(data["line_id"]),
            reference_id=str(data["reference_id"]),
            text=str(data["text"]),
            speed=float(data.get("speed", 1.0)),
            language=str(data.get("language", "Chinese")),
            randomness=str(data.get("randomness", "normal")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
