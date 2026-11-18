"""자동 루프의 틱 판정(runner.decide) — 시각 의존 로직이라 순수 함수로 고정한다.

퇴근 시각이 매일 달라지는 설계라(부팅+근무시간) 여기가 틀리면 "어느 날은 안 돈다"가
된다. 스케줄러를 안 쓰기로 한 판단의 근거가 이 함수 하나에 몰려 있다.
"""
from __future__ import annotations

import datetime as dt

from dooray_sync.auto import cli as auto_cli
from dooray_sync.auto import launcher, runner
from dooray_sync.auto.day import current_day, starts_new_day, summary
from dooray_sync.auto.runner import advance_day, decide
from dooray_sync.auto.state import AutoState, auto_dir, save_state

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


# ---------------------------------------------------------------------------
# 밤새 켜 둔 PC — 근무일은 자정이 아니라 기동 시각에서 24시간씩 넘어간다.
#
# 2026-11-11 실측(개발 PC, state.json 읽기 전용): day_start 11-10 07:08:58,
# last_eod_date 11-11, 모든 프로파일 보고가 11-11 00:01. 퇴근 스윕을 마쳤는지를
# '지금'의 날짜로 판정해, 자정을 넘는 순간 어제의 15:53 스윕이 다시 돌고 오늘
# 날짜를 기록해 **오늘의 진짜 퇴근 스윕을 지웠다.** 머리글·status.txt·
# `--auto status`는 어제의 15:53을 오늘 것처럼 '(완료)'로 보였다.
# ---------------------------------------------------------------------------

TUE = dt.datetime(2026, 11, 10, 7, 8, 58)    # 실측 사고의 기동 시각(화요일)
TICK = dt.timedelta(seconds=120)
EOD_CLOCK = dt.time(15, 53, 58)              # 07:08:58 + 9시간 - 15분


def _simulate(st: AutoState, start: dt.datetime, end: dt.datetime, *,
              asleep: tuple = ()) -> list[tuple[str, dt.datetime]]:
    """루프를 TICK 간격으로 돌린다 — _tick과 같은 순서(decide → advance_day →
    last_tick). 매 틱마다 **표시가 판정과 같은 말을 하는지**도 함께 검사한다.
    asleep: 틱이 없는 구간들(절전·전원 꺼짐)."""
    events = []
    now = start
    while now <= end:
        if not any(a <= now < b for a, b in asleep):
            d = decide(st, AUTO, ["a"], now=now, last_sync={"a": now})
            if not starts_new_day(st, AUTO, now):
                _, eod_text = summary(st, AUTO, now)
                assert ("다음 틱에 실행" in eod_text) == (d.kind == "sweep_eod"), (now, eod_text, d)
            if d.kind in ("sweep_start", "sweep_eod"):
                events.append((d.kind, now))
            advance_day(st, d, AUTO, now)
            st.last_tick = now.isoformat(timespec="seconds")
        now += TICK
    return events


def _resume(st: AutoState, until: dt.datetime) -> list[tuple[str, dt.datetime]]:
    """마지막 틱 다음부터 이어서 돌린다."""
    return _simulate(st, runner._parse(st.last_tick) + TICK, until)


def test_overnight_pc_does_not_sweep_at_midnight():
    """실측 상태 그대로 — 어제 15:53 스윕을 마쳤고 밤새 2분마다 틱이 돌았다."""
    st = _st(day_start=TUE.isoformat(), last_eod_date="2026-11-10",
             last_tick="2026-11-10T23:59:58")
    now = dt.datetime(2026, 11, 11, 0, 1, 58)
    d = decide(st, AUTO, ["a"], now=now, last_sync={"a": now - dt.timedelta(minutes=5)})
    assert d.kind == "idle", d


def test_overnight_pc_sweeps_once_per_weekday_at_the_same_time():
    """한 주 내내 안 끈 PC: 기동은 첫날 한 번, 퇴근 스윕은 평일마다 같은 시각에 한 번.
    자정·새벽에는 어떤 스윕도 없고 주말에는 퇴근 스윕이 없다."""
    st = _st()
    events = _simulate(st, TUE, TUE + dt.timedelta(days=7, hours=12))

    assert [t for k, t in events if k == "sweep_start"] == [TUE]
    eods = [t for k, t in events if k == "sweep_eod"]
    assert [t.day for t in eods] == [10, 11, 12, 13, 16, 17]     # 토(14)·일(15) 없음
    for t in eods:
        late = t - dt.datetime.combine(t.date(), EOD_CLOCK)
        assert dt.timedelta(0) <= late < TICK, t                 # 매일 15:53 직후 첫 틱
    assert not [e for e in events if e[1].hour < 7], events


def test_overnight_pc_next_day_is_defined_and_displayed_honestly():
    """자정 직후에는 어제의 스윕을 날짜와 함께 보이고, 기동 시각이 지나면 오늘의
    퇴근 스윕을 '가정'이라고 밝히며 예정으로 보인다. 그 시각에 실제로 돈다."""
    st = _st()
    _simulate(st, TUE, dt.datetime(2026, 11, 11, 0, 2))
    now = dt.datetime(2026, 11, 11, 0, 2)
    start_text, eod_text = summary(st, AUTO, now)
    assert eod_text == "11-10 15:53 (완료)"          # 오늘의 '15:53 (완료)'가 아니다
    assert start_text == "11-10 07:08"
    assert not current_day(st, AUTO, now).pending(now)   # 대기 줄에 퇴근 스윕 없음

    _resume(st, dt.datetime(2026, 11, 11, 10, 0))
    now = dt.datetime(2026, 11, 11, 10, 0, 30)
    start_text, eod_text = summary(st, AUTO, now)
    assert eod_text == "15:53 (예정)"
    assert start_text.startswith("07:08 (11-10 07:08 기동 후") and "가정" in start_text
    assert current_day(st, AUTO, now).pending(now)

    events = _resume(st, dt.datetime(2026, 11, 11, 16, 30))
    assert [k for k, _t in events] == ["sweep_eod"]
    assert summary(st, AUTO, dt.datetime(2026, 11, 11, 16, 31))[1] == "15:53 (완료)"
    assert st.last_eod_date == "2026-11-11"


def test_evening_boot_keeps_its_own_eod_sweep_after_midnight():
    """16:00에 켠 PC의 퇴근 스윕은 다음 날 00:45다 — 그건 자정 오인이 아니라 그 근무일의
    스윕이므로 돌아야 한다. 'day_start가 오늘일 때만'으로 막으면 이게 사라진다."""
    boot = dt.datetime(2026, 11, 10, 16, 0, 0)
    st = _st()
    events = _simulate(st, boot, dt.datetime(2026, 11, 12, 2, 0))
    assert [k for k, _t in events] == ["sweep_start", "sweep_eod", "sweep_eod"]
    assert [f"{t:%m-%d %H:%M}" for k, t in events if k == "sweep_eod"] == [
        "11-11 00:46", "11-12 00:46"]


def test_real_boot_replaces_the_carried_schedule():
    """이어 붙인 근무일은 진짜 기동이 오면 끝난다 — 그날부터 새 기동 시각이 기준."""
    st = _st()
    asleep = ((dt.datetime(2026, 11, 11, 18, 0), dt.datetime(2026, 11, 12, 8, 30)),)
    events = _simulate(st, TUE, dt.datetime(2026, 11, 12, 18, 0), asleep=asleep)
    assert [(k, f"{t:%m-%d %H:%M}") for k, t in events] == [
        ("sweep_start", "11-10 07:08"),
        ("sweep_eod", "11-10 15:54"),
        ("sweep_eod", "11-11 15:54"),
        ("sweep_start", "11-12 08:30"),
        ("sweep_eod", "11-12 17:16"),          # 08:30:58 + 9시간 - 15분 = 17:15:58
    ]


def test_header_after_overnight_shutdown_previews_the_new_start():
    """아침에 창을 띄우면 첫 틱이 기동이다 — 머리글은 어제의 15:53이 아니라 지금 기준
    시각을 보인다. 창이 닫힌 채 조회하면(status) '다음 틱에서 정함'으로 적는다."""
    st = _st(day_start=TUE.isoformat(), last_eod_date="2026-11-10",
             last_tick="2026-11-10T18:00:00")
    now = dt.datetime(2026, 11, 11, 8, 0)
    assert summary(st, AUTO, now, tick_now=True) == (
        "08:00 (첫 틱이 기동)", "16:45 예정(첫 틱에 확정)")
    assert summary(st, AUTO, now) == (
        "다음 틱에서 새로 정함 (지난 기동 11-10 07:08)", "다음 기동 때 정해짐")


def _overnight_state_at(now: dt.datetime) -> AutoState:
    st = _st()
    _simulate(st, TUE, now)
    return st


def test_status_file_agrees_with_decide():
    now = dt.datetime(2026, 11, 11, 0, 2)
    st = _overnight_state_at(now)
    runner._write_status(st, AUTO, [], now)
    text = (auto_dir() / "status.txt").read_text(encoding="utf-8")
    assert "기동     : 11-10 07:08\n" in text
    assert "퇴근 스윕: 11-10 15:53 (완료)\n" in text


def test_auto_status_agrees_with_decide(monkeypatch, capsys):
    """`--auto status`도 같은 함수를 본다 — 기본 설정(config 없음)의 근무시간 9시간."""
    now = dt.datetime(2026, 11, 11, 10, 0, 30)
    st = _overnight_state_at(now)
    save_state(st.data)
    monkeypatch.setattr(auto_cli, "_now", lambda: now)
    monkeypatch.setattr(launcher, "describe", lambda: (False, "등록 안 됨"))
    assert auto_cli._status([], True) == 0
    out = capsys.readouterr().out
    assert "  퇴근 스윕    : 15:53 (예정)\n" in out
    assert "  기동         : 07:08 (11-10 07:08 기동 후" in out
