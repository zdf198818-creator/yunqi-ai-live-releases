from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Literal

PAUSE_PATTERN = re.compile(r"#(\d+)#")
RANDOM_CHOICE_PATTERN = re.compile(r"\[([^\[\]]+)\]|【([^【】]+)】")
SPECIAL_MARKER_PATTERN = re.compile(r"#\d+#|\[[^\[\]]+\]|【[^【】]+】")
SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\r\n]+[。！？!?；;]*")
MAX_PAUSE_MS = 60_000


def resolve_random_choices(text: str) -> str:
    """Choose one pipe-separated option from every bracket group."""

    def choose(match: re.Match[str]) -> str:
        content = match.group(1) if match.group(1) is not None else match.group(2)
        choices = [
            choice.strip()
            for choice in re.split(r"[|｜]", content)
            if choice.strip()
        ]
        if not choices:
            raise ValueError("随机词组不能为空")
        return secrets.choice(choices)

    return RANDOM_CHOICE_PATTERN.sub(choose, text)


@dataclass(frozen=True, slots=True)
class ScriptToken:
    kind: Literal["speech", "pause"]
    text: str = ""
    duration_ms: int = 0


def split_script_sentences(text: str) -> list[str]:
    """Split pasted/imported copy into one table row per sentence."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    sentences: list[str] = []
    for physical_line in normalized.split("\n"):
        for match in SENTENCE_PATTERN.finditer(physical_line):
            sentence = match.group(0).strip()
            if sentence:
                sentences.append(sentence)
    return sentences


def parse_script(text: str) -> list[ScriptToken]:
    """Split one script line into TTS text and exact millisecond pauses."""
    text = resolve_random_choices(text)
    tokens: list[ScriptToken] = []
    cursor = 0

    for match in PAUSE_PATTERN.finditer(text):
        speech = text[cursor : match.start()].strip()
        if speech:
            tokens.append(ScriptToken(kind="speech", text=speech))

        duration_ms = int(match.group(1))
        if duration_ms > MAX_PAUSE_MS:
            raise ValueError(f"单个停顿不能超过{MAX_PAUSE_MS}毫秒")
        tokens.append(ScriptToken(kind="pause", duration_ms=duration_ms))
        cursor = match.end()

    trailing = text[cursor:].strip()
    if trailing:
        tokens.append(ScriptToken(kind="speech", text=trailing))

    if not tokens:
        raise ValueError("话术没有可播放内容")
    return tokens
