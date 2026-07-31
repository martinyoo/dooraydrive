"""상태 저장소 패키지 — SQLite(WAL) 기반 files/journal/conflicts/meta."""
from __future__ import annotations

from .db import (
    JOURNAL_PHASES,
    META_CURSOR_FILE_ID,
    META_CURSOR_REVISION,
    META_HASH_ALGO,
    META_LAST_FULL_SCAN,
    SYNC_STATUSES,
    TERMINAL_PHASES,
    FileRecord,
    Store,
    now_iso,
)

__all__ = [
    "Store",
    "FileRecord",
    "SYNC_STATUSES",
    "JOURNAL_PHASES",
    "TERMINAL_PHASES",
    "META_CURSOR_REVISION",
    "META_CURSOR_FILE_ID",
    "META_LAST_FULL_SCAN",
    "META_HASH_ALGO",
    "now_iso",
]
