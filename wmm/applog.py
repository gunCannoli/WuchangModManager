"""Timestamped application log: in-memory rows for the Log dialog plus an append-only file under data/logs/."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class LogEntry:
    when: datetime
    level: str
    message: str

    @property
    def stamp(self) -> str:
        return self.when.strftime("%Y-%m-%d %H:%M:%S")

    def line(self) -> str:
        return f"{self.stamp}  {self.level:<5}  {self.message}"


class AppLog:
    def __init__(self, file_path: Optional[Path] = None):
        self.entries: list[LogEntry] = []
        self.file_path = Path(file_path) if file_path else None

    def add(self, message: str, level: str = "INFO") -> LogEntry:
        entry = LogEntry(datetime.now(), level.upper(), message)
        self.entries.append(entry)
        if self.file_path is not None:
            try:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                with self.file_path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(entry.line() + "\n")
            except OSError:
                pass
        return entry

    def text(self) -> str:
        return "\n".join(e.line() for e in self.entries)
