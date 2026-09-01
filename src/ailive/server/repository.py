from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from ailive.domain import VoiceProfile


class VoiceRepository:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.audio_dir = data_dir / "voices"
        self.database_path = data_dir / "ailive.sqlite3"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                    CREATE TABLE IF NOT EXISTS voices (
                        reference_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        audio_path TEXT NOT NULL,
                        reference_text TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
            )

    def create(self, name: str, reference_text: str, suffix: str, content: bytes) -> VoiceProfile:
        reference_id = f"voice-{uuid.uuid4().hex[:12]}"
        safe_suffix = (
            suffix.lower() if suffix.lower() in {".wav", ".mp3", ".flac", ".m4a"} else ".wav"
        )
        audio_path = self.audio_dir / f"{reference_id}{safe_suffix}"
        audio_path.write_bytes(content)

        profile = VoiceProfile(
            reference_id=reference_id,
            name=name.strip(),
            audio_path=str(audio_path),
            reference_text=reference_text.strip(),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO voices(reference_id, name, audio_path, reference_text) VALUES (?, ?, ?, ?)",
                (
                    profile.reference_id,
                    profile.name,
                    profile.audio_path,
                    profile.reference_text,
                ),
            )
        return profile

    def list_all(self) -> list[VoiceProfile]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT reference_id, name, audio_path, reference_text FROM voices ORDER BY created_at"
            ).fetchall()
        return [VoiceProfile(**dict(row)) for row in rows]

    def get(self, reference_id: str) -> VoiceProfile | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT reference_id, name, audio_path, reference_text FROM voices WHERE reference_id = ?",
                (reference_id,),
            ).fetchone()
        return VoiceProfile(**dict(row)) if row else None
