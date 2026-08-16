r"""마지막 동기화 시각 조회 — 실패를 '한 번도 안 함'으로 읽지 않는다.

2026-08-16 실측 사고: SQLite URI를 확장 길이 경로(\\?\)로 만들어
`invalid uri authority: %3f`로 연결이 항상 실패했고, 그 실패를 None으로
삼켜 "한 번도 동기화한 적 없음 → 즉시 실행"으로 읽었다. 결과로 30분 주기가
통째로 무력해지고 2분마다 실제 동기화가 돌았다(원격 호출 15배).

고장을 '즉시 실행'으로 번역하는 코드는 고장을 폭주로 바꾼다. 여기서 그 성질을
고정한다.
"""
from __future__ import annotations

import sqlite3

import pytest

from dooray_sync import config as cfg
from dooray_sync.auto.runner import _last_sync_at, _last_sync_map, _ro_uri
from dooray_sync.store.db import Store


@pytest.fixture()
def statedir(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(tmp_path / "cfg"))
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(tmp_path / "state"))
    return tmp_path


def test_ro_uri_has_no_extended_prefix():
    """URI에 \\\\?\\ 가 섞이면 sqlite가 authority로 오해해 연결 자체가 실패한다."""
    uri = _ro_uri(cfg.db_path("p"))
    assert uri.startswith("file:")
    assert "?mode=ro" in uri
    assert "//?/" not in uri and "%3f" not in uri.lower().replace("%3fmode", "")
    assert "\\" not in uri


def test_reads_recorded_time(statedir):
    with Store(cfg.db_path("p")) as store:
        store.set_meta("last_sync_at", "2026-08-16T15:36:58")
    got = _last_sync_at("p")
    assert got is not None and got.hour == 15 and got.minute == 36


def test_missing_db_is_none(statedir):
    """DB가 없으면 진짜로 '한 번도 안 함'이다 — 이것만 None이 맞다."""
    assert _last_sync_at("nodb") is None


def test_never_synced_db_is_none(statedir):
    with Store(cfg.db_path("p")):
        pass
    assert _last_sync_at("p") is None


def test_read_failure_raises_not_none(statedir):
    """읽기 실패는 예외로 올라와야 한다. None으로 삼키면 즉시 실행이 된다."""
    path = cfg.db_path("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        _last_sync_at("broken")


def test_map_drops_failing_profile(statedir):
    """조회 실패 프로파일은 이번 틱 대상에서 빠진다(fail-closed) — 모른다는
    이유로 실행하면 고장이 곧 폭주다."""
    with Store(cfg.db_path("good")) as store:
        store.set_meta("last_sync_at", "2026-08-16T10:00:00")
    bad = cfg.db_path("bad")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not sqlite")

    got = _last_sync_map(["good", "bad"])

    assert "good" in got
    assert "bad" not in got
