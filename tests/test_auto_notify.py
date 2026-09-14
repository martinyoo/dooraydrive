"""통지 — 무인 실행이 사람에게 남기는 말(M3 단위 12).

핵심 성질 둘을 고정한다.
  1. 같은 (프로파일, 종류)는 쌓이지 않는다 — 2분마다 같은 줄이 붙으면 목록이
     노이즈가 되고 그 노이즈가 진짜 신호를 묻는다.
  2. 원인이 해소되면 스스로 사라진다 — '확인' 버튼 방식이면 아무도 안 누른다.
"""
from __future__ import annotations

import datetime as dt

import pytest

from dooray_sync import config as cfg
from dooray_sync.auto import notify
from dooray_sync.auto.runner import _note_outcome
from dooray_sync.auto.state import AutoState

NOW = dt.datetime(2026, 8, 17, 9, 0, 0)


@pytest.fixture()
def statedir(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(tmp_path / "cfg"))
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(tmp_path / "state"))
    return tmp_path


def test_same_kind_is_replaced_not_appended(statedir):
    for i in range(5):
        notify.add("a", "held", f"보류 {i}")
    items = notify.load()
    assert len(items) == 1
    assert items[0]["message"] == "보류 4"


def test_different_kinds_coexist(statedir):
    notify.add("a", "held", "보류")
    notify.add("a", "deletes", "삭제 대기")
    notify.add("b", "held", "다른 폴더")
    assert len(notify.load()) == 3


def test_clear_by_profile_and_kind(statedir):
    notify.add("a", "held", "x")
    notify.add("a", "deletes", "y")
    notify.clear("a", "held")
    kinds = {i["kind"] for i in notify.load()}
    assert kinds == {"deletes"}
    notify.clear("a")
    assert notify.load() == []


def test_format_block_empty_is_blank(statedir):
    assert notify.format_block() == ""


def test_format_block_lists_items(statedir):
    notify.add("spri2026", "held", "로컬 폴더가 비어 보입니다", ts="2026-08-17T09:00:00")
    block = notify.format_block()
    assert "자동 동기화 알림 1건" in block
    assert "[보류]" in block
    assert "spri2026" in block


def test_held_outcome_creates_notice_and_success_clears(statedir):
    """보류 → 통지 생성, 다음 성공 → 자동 소멸."""
    st = AutoState({})
    _note_outcome(st, "a", "held", "보류", {"held_reason": "local_collapse"}, NOW)
    items = notify.load()
    assert len(items) == 1 and items[0]["kind"] == "held"
    assert "드라이브" in items[0]["message"]

    _note_outcome(st, "a", "ok", "완료", {}, NOW)
    assert notify.load() == []


def test_deferred_deletes_notified_then_cleared(statedir):
    st = AutoState({})
    _note_outcome(st, "a", "ok", "완료", {"deletes": {"deferred": 7}}, NOW)
    items = notify.load()
    assert len(items) == 1 and items[0]["kind"] == "deletes"
    assert "7건" in items[0]["message"]

    _note_outcome(st, "a", "ok", "완료", {"deletes": {"deferred": 0}}, NOW)
    assert notify.load() == []


def test_conflicts_deferred_notified(statedir):
    st = AutoState({})
    _note_outcome(st, "a", "ok", "완료", {"conflicts_deferred": 3}, NOW)
    items = notify.load()
    assert len(items) == 1 and items[0]["kind"] == "conflicts"
    assert "개명" in items[0]["message"]


def test_config_error_parks_profile_until_config_changes(statedir):
    """설정 오류는 같은 거부를 2분마다 반복하지 않는다 — config가 바뀌면 재개."""
    from dooray_sync.auto.runner import _skipped_by_config_error

    cfg.save_config(cfg.Profile(name="a", drive_id="d",
                                local_root=str(statedir / "a")))
    st = AutoState({})
    _note_outcome(st, "a", "config", "설정 문제", {}, NOW)

    assert _skipped_by_config_error(st, "a") is True
    assert [i["kind"] for i in notify.load()] == ["config"]

    # config가 바뀌면 자동으로 풀린다
    cfg.save_config(cfg.Profile(name="b", drive_id="d",
                                local_root=str(statedir / "b")))
    assert _skipped_by_config_error(st, "a") is False


def test_config_change_is_seen_even_when_mtime_did_not_move(statedir):
    """시계가 안 움직여도 내용이 바뀌었으면 풀려야 한다.

    이 판정이 mtime 이던 시절의 실측(2026-09-14): 내용이 매번 달라지는 저장
    300회 중 **mtime 은 변경을 173회(58%) 놓쳤다**(내용 지문은 0회). Windows
    시스템 시계는 약 15.6ms 단위로 움직이는데 config 저장은 그보다 훨씬 빨리
    끝난다. 그래서 "config 가 바뀌면 재개한다"가 동전 던지기였고, 위 테스트가
    전체 실행에서 간헐적으로 빨간불이 됐다 — 릴리스 판정이 흔들린다는 뜻이다.

    여기서는 mtime 을 **강제로 되돌려** 그 조건을 확실히 만든다. 안전장치는
    그것이 필요한 조건에서 검증해야 한다(vault: vault-sync-script-silent-commit-loss).
    """
    import os

    from dooray_sync.auto.runner import _skipped_by_config_error
    from dooray_sync.util.paths import ext_path

    cfg.save_config(cfg.Profile(name="a", drive_id="d",
                                local_root=str(statedir / "a")))
    path = ext_path(cfg.config_path())
    before = os.stat(path)

    st = AutoState({})
    _note_outcome(st, "a", "config", "설정 문제", {}, NOW)
    assert _skipped_by_config_error(st, "a") is True

    cfg.save_config(cfg.Profile(name="b", drive_id="d",
                                local_root=str(statedir / "b")))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(path).st_mtime_ns == before.st_mtime_ns, "전제가 안 만들어졌다"

    assert _skipped_by_config_error(st, "a") is False
