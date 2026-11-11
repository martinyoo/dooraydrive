"""자동 루프의 틱 판정(runner.decide) — 시각 의존 로직이라 순수 함수로 고정한다.

퇴근 시각이 매일 달라지는 설계라(부팅+근무시간) 여기가 틀리면 "어느 날은 안 돈다"가
된다. 스케줄러를 안 쓰기로 한 판단의 근거가 이 함수 하나에 몰려 있다.
"""
from __future__ import annotations

import datetime as dt

import pytest

from dooray_sync.auto.runner import decide
from dooray_sync.auto.state import AutoState

AUTO = {
    "work_hours": 9.0,
    "tick_sec": 120,
    "min_interval_sec": 1800,
    "day_gap_hours": 6.0,
    "max_auto_deletes": 5,
    "max_auto_delete_mb": 50,
}

MON = dt.datetime(2026, 8, 17, 7, 0, 0)      # 월요일 07:00
SAT = dt.datetime(2026, 8, 15, 7, 0, 0)      # 토요일 07:00


def _st(**data) -> AutoState:
    return AutoState(data)


def test_first_tick_ever_is_start_sweep():
    d = decide(_st(), AUTO, ["a", "b"], now=MON, last_sync={"a": None, "b": None})
    assert d.kind == "sweep_start"
    assert d.names == ["a", "b"]              # 스윕은 전체를 한 바퀴


def test_gap_over_threshold_is_start_sweep():
    """밤새 꺼져 있었다 / 절전에서 깨어났다 — 둘 다 '기동'으로 잡힌다."""
    st = _st(last_tick=(MON - dt.timedelta(hours=14)).isoformat())
    d = decide(st, AUTO, ["a"], now=MON, last_sync={"a": MON - dt.timedelta(hours=14)})
    assert d.kind == "sweep_start"


def test_short_gap_is_not_start_sweep():
    """2분 전에 돌았으면 기동이 아니다 — 매 틱 스윕하면 30분 주기가 무의미해진다."""
    st = _st(last_tick=(MON - dt.timedelta(minutes=2)).isoformat(),
             day_start=MON.isoformat())
    d = decide(st, AUTO, ["a"], now=MON, last_sync={"a": MON})
    assert d.kind == "idle"


def test_eod_sweep_fires_15min_before_work_end():
    """07:00 기동 + 9시간 = 16:00 퇴근 → 15:45에 스윕."""
    st = _st(last_tick=(MON.replace(hour=15, minute=43)).isoformat(),
             day_start=MON.isoformat())
    before = decide(st, AUTO, ["a"], now=MON.replace(hour=15, minute=44),
                    last_sync={"a": MON.replace(hour=15, minute=40)})
    assert before.kind != "sweep_eod"

    at = decide(st, AUTO, ["a"], now=MON.replace(hour=15, minute=45),
                last_sync={"a": MON.replace(hour=15, minute=40)})
    assert at.kind == "sweep_eod"
    assert "15:45" in at.why


def test_eod_sweep_only_once_per_day():
    st = _st(last_tick=(MON.replace(hour=15, minute=45)).isoformat(),
             day_start=MON.isoformat(),
             last_eod_date=MON.date().isoformat())
    d = decide(st, AUTO, ["a"], now=MON.replace(hour=16, minute=30),
               last_sync={"a": MON.replace(hour=16, minute=20)})
    assert d.kind != "sweep_eod"


def test_no_eod_sweep_on_weekend():
    """토·일은 퇴근 스윕 없음 — 출근 스윕과 30분 평시만 돈다(사용자 결정)."""
    st = _st(last_tick=(SAT.replace(hour=15, minute=43)).isoformat(),
             day_start=SAT.isoformat())
    d = decide(st, AUTO, ["a"], now=SAT.replace(hour=16, minute=0),
               last_sync={"a": SAT.replace(hour=15, minute=55)})
    assert d.kind != "sweep_eod"


def test_work_hours_change_takes_effect_without_reregistration():
    """근무시간을 6으로 바꾸면 12:45에 퇴근 스윕 — 재등록 없이 즉시 반영."""
    st = _st(last_tick=(MON.replace(hour=12, minute=43)).isoformat(),
             day_start=MON.isoformat())
    six = dict(AUTO, work_hours=6.0)
    d = decide(st, six, ["a"], now=MON.replace(hour=12, minute=45),
               last_sync={"a": MON.replace(hour=12, minute=40)})
    assert d.kind == "sweep_eod"


def test_due_picks_single_oldest_profile():
    """평시는 틱당 1개 — 가장 오래 밀린 것부터(기아 방지)."""
    st = _st(last_tick=(MON.replace(hour=10)).isoformat(), day_start=MON.isoformat())
    now = MON.replace(hour=10, minute=2)
    d = decide(st, AUTO, ["a", "b", "c"], now=now, last_sync={
        "a": now - dt.timedelta(minutes=40),
        "b": now - dt.timedelta(minutes=90),      # 가장 오래됨
        "c": now - dt.timedelta(minutes=10),      # 아직 30분 안 됨
    })
    assert d.kind == "due"
    assert d.names == ["b"]


def test_due_respects_interval():
    st = _st(last_tick=(MON.replace(hour=10)).isoformat(), day_start=MON.isoformat())
    now = MON.replace(hour=10, minute=2)
    d = decide(st, AUTO, ["a"], now=now,
               last_sync={"a": now - dt.timedelta(minutes=20)})
    assert d.kind == "idle"


def test_backoff_multiplier_extends_interval():
    """429를 맞은 프로파일은 주기가 늘어난 만큼 늦게 차례가 온다."""
    st = _st(last_tick=(MON.replace(hour=10)).isoformat(), day_start=MON.isoformat(),
             profiles={"a": {"backoff_mult": 4.0}})
    now = MON.replace(hour=10, minute=2)
    seen = now - dt.timedelta(minutes=40)          # 30분은 지났지만 120분은 아니다
    assert decide(st, AUTO, ["a"], now=now, last_sync={"a": seen}).kind == "idle"
    old = now - dt.timedelta(minutes=125)
    assert decide(st, AUTO, ["a"], now=now, last_sync={"a": old}).kind == "due"


def test_never_synced_profile_is_due_immediately():
    st = _st(last_tick=(MON.replace(hour=10)).isoformat(), day_start=MON.isoformat())
    d = decide(st, AUTO, ["fresh"], now=MON.replace(hour=10, minute=2),
               last_sync={"fresh": None})
    assert d.kind == "due" and d.names == ["fresh"]


def test_missing_day_start_forces_start_sweep():
    """day_start가 없으면 기동으로 친다 — 없으면 퇴근 스윕의 기준점이 없어
    그날 퇴근 스윕이 영영 안 뜬다. '--auto now'가 last_tick만 남기거나 상태
    파일이 지워진 경우가 정확히 그 상태다(자기 치유)."""
    st = _st(last_tick=(MON.replace(hour=10)).isoformat())   # day_start 없음
    d = decide(st, AUTO, ["a"], now=MON.replace(hour=10, minute=2),
               last_sync={"a": MON.replace(hour=10)})
    assert d.kind == "sweep_start"


def _held(now: dt.datetime, at: dt.datetime | str, mult: float = 8.0) -> AutoState:
    st = _st(last_tick=(now - dt.timedelta(minutes=2)).isoformat(),
             day_start=now.replace(hour=7, minute=0).isoformat())
    stamp = at if isinstance(at, str) else at.isoformat(timespec="seconds")
    st.profile("w")["bulk_hold"] = {"why": "삭제 대상 56건", "count": 56, "at": stamp}
    st.set_backoff_mult("w", mult)
    return st


def test_bulk_held_profile_counts_from_the_hold_not_the_last_sync():
    """대량 삭제 보류는 last_sync_at 을 남기지 않는다 — 마지막 동기화가 닷새 전이어도
    보류 직후에는 차례가 아니다. 2026-11-11 실측: 이 판정이 없어 WRITING 이 2분마다
    776회 다시 돌았다."""
    now = MON.replace(hour=10)
    stale = now - dt.timedelta(days=5)
    st = _held(now, now - dt.timedelta(minutes=2))
    assert decide(st, AUTO, ["w"], now=now, last_sync={"w": stale}).kind == "idle"
    just_before = now + dt.timedelta(hours=4, minutes=-3)
    st.last_tick = (just_before - dt.timedelta(minutes=2)).isoformat()
    assert decide(st, AUTO, ["w"], now=just_before, last_sync={"w": stale}).kind == "idle"
    after = now + dt.timedelta(hours=4)                      # 보류 시각 + 30분 x8
    st.last_tick = (after - dt.timedelta(minutes=2)).isoformat()
    d = decide(st, AUTO, ["w"], now=after, last_sync={"w": stale})
    assert d.kind == "due" and d.names == ["w"]


def test_bulk_held_profile_that_never_synced_is_not_due_at_once():
    now = MON.replace(hour=10)
    st = _held(now, now - dt.timedelta(minutes=2), mult=1.0)
    assert decide(st, AUTO, ["w"], now=now, last_sync={"w": None}).kind == "idle"


def test_bulk_held_profile_does_not_take_other_profiles_turn():
    now = MON.replace(hour=10)
    st = _held(now, now - dt.timedelta(minutes=2))
    d = decide(st, AUTO, ["a", "w"], now=now,
               last_sync={"a": now - dt.timedelta(minutes=40), "w": now - dt.timedelta(days=5)})
    assert d.kind == "due" and d.names == ["a"]


@pytest.mark.parametrize("at", ["", "언젠가", "2026-08-17T99:00:00"])
def test_unreadable_hold_time_falls_back_to_the_last_sync(at):
    now = MON.replace(hour=10)
    st = _held(now, at)
    d = decide(st, AUTO, ["w"], now=now, last_sync={"w": now - dt.timedelta(days=5)})
    assert d.kind == "due"


def test_hold_time_in_the_future_is_ignored():
    """시계가 뒤로 간 PC — 미래의 보류 시각에 묶이면 시계가 따라잡을 때까지 안 돈다."""
    now = MON.replace(hour=10)
    st = _held(now, now + dt.timedelta(days=2))
    d = decide(st, AUTO, ["w"], now=now, last_sync={"w": now - dt.timedelta(days=5)})
    assert d.kind == "due"


def test_sweeps_still_include_a_held_profile():
    """출근·퇴근 스윕은 전체 한 바퀴다 — 보류된 폴더도 그때는 다시 확인한다."""
    now = MON.replace(hour=15, minute=45)
    st = _held(now, now - dt.timedelta(minutes=2))
    d = decide(st, AUTO, ["a", "w"], now=now,
               last_sync={"a": now - dt.timedelta(minutes=5), "w": now - dt.timedelta(days=5)})
    assert d.kind == "sweep_eod" and d.names == ["a", "w"]


def test_no_auto_profiles_is_idle():
    d = decide(_st(), AUTO, [], now=MON, last_sync={})
    assert d.kind == "idle"
    assert "없습니다" in d.why
