"""자동 루프의 틱 판정(runner.decide) — 시각 의존 로직이라 순수 함수로 고정한다.

퇴근 시각이 매일 달라지는 설계라(부팅+근무시간) 여기가 틀리면 "어느 날은 안 돈다"가
된다. 스케줄러를 안 쓰기로 한 판단의 근거가 이 함수 하나에 몰려 있다.
"""
from __future__ import annotations

import datetime as dt

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


def test_no_auto_profiles_is_idle():
    d = decide(_st(), AUTO, [], now=MON, last_sync={})
    assert d.kind == "idle"
    assert "없습니다" in d.why
