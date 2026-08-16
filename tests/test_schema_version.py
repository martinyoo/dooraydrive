"""DB 스키마 버전(PRAGMA user_version) 규약 — 배포 전략 항목 D.

무인 상시 실행(M3)은 "컬럼을 추가한 릴리스가 기존 PC에서 조용히 깨지는" 사고를
상시화하므로, 버전 어긋남이 조용히 통과하지 않는 것을 여기서 고정한다.
"""
from __future__ import annotations

import sqlite3

import pytest

from dooray_sync.store.db import SCHEMA_VERSION, SchemaVersionError, Store


def test_fresh_db_gets_stamped(tmp_path):
    """신규 DB는 현재 스키마 버전으로 스탬프된다."""
    db = tmp_path / "state.db"
    with Store(db) as store:
        got = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert got == SCHEMA_VERSION


def test_legacy_db_is_promoted(tmp_path):
    """버전 도입 전(user_version=0) DB — 스키마 동일하므로 승격만 되고 데이터 보존."""
    db = tmp_path / "state.db"
    with Store(db) as store:            # 스키마 생성
        store._conn.execute("PRAGMA user_version = 0")   # 도입 전 상태로 되돌림
        store._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('probe', 'kept')")
        store._conn.commit()
    with Store(db) as store:            # 재개방 = 승격 경로
        got = store._conn.execute("PRAGMA user_version").fetchone()[0]
        kept = store._conn.execute(
            "SELECT value FROM meta WHERE key='probe'").fetchone()[0]
    assert got == SCHEMA_VERSION
    assert kept == "kept"


def test_newer_db_fails_stop(tmp_path):
    """코드보다 새 DB는 조용히 통과하지 않는다 — 명시적 오류(fail-stop)."""
    db = tmp_path / "state.db"
    with Store(db):
        pass
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(SchemaVersionError) as exc:
        Store(db)
    assert "새 버전" in str(exc.value)
    assert str(db) in str(exc.value)
