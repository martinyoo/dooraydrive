"""연속 실패의 주기 — 진짜 decide 로 여러 시간의 틱을 돌려 본다 (2026-11-11).

자동 루프는 프로파일의 차례를 상태 DB 의 last_sync_at 으로 셌다. 실패한 실행은 그 값을
남기지 않으므로, 마지막 성공이 '주기 x8'(30분이면 4시간)보다 오래되면 기준점이 언제나
지나 있어 그 폴더가 **매 틱** 다시 돌았다. 대량 삭제 보류는 그날 먼저 고쳤고(WRITING
776회), 인증 오류·Dooray 거부·장애·네트워크 끊김·로컬 붕괴 보류에는 같은 구멍이 남아
있었다 — `_apply_backoff`의 "3회 연속부터 2배씩"은 몇 시간 뒤부터 거짓말이었다.

여기서 지키는 것:
  1. 연속 3회 실패부터는 **마지막 시도**부터 센다 — 간격 x2, x4, x8, x8, … 틱마다는 다시 없다.
  2. 처음 두 번의 실패는 틱 간격으로 다시 시도한다 — 잠깐의 끊김은 몇 분 안에 회복한다.
  3. 화면의 '(연속 N회 …)' 줄, 대기 줄의 다음 동기화, status.txt 가 적는 시각은 decide 가
     실제로 돌리는 시각과 같다.
  4. 시도 시각은 state.json 에 남아 창을 다시 띄워도 이어진다.
  5. 설정 오류로 쉬는 폴더는 다른 폴더의 차례를 빼앗지 않는다 — 예전에는 decide 가 그 폴더를
     '가장 오래 밀린' 것으로 매 틱 고르고 _tick 이 건너뛰어, 다른 폴더가 3시간 동안 0회 돌았다.

판정(decide)은 **갈아 끼우지 않는다.** 0.3.3 의 틱 테스트는 전부 decide 를 가짜로 바꿔 매 틱
다시 도는 것을 보지 못했다(AGENTS.md — 틱 테스트에는 decide 를 바꾸지 않는 것을 하나 이상).
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dooray_sync.auto import cli as auto_cli                      # noqa: E402
from dooray_sync.auto import keys, runner                         # noqa: E402
from dooray_sync.config import config_path                        # noqa: E402
from dooray_sync.auto.state import AutoState, auto_dir, state_path  # noqa: E402
from dooray_sync.util.paths import ext_path                       # noqa: E402

NOW = dt.datetime(2026, 11, 10, 10, 56)          # 화요일
TICK = dt.timedelta(minutes=2)
STALE = dt.datetime(2026, 11, 5, 9, 12)          # 닷새 전의 마지막 성공
AUTO = {"tick_sec": 120, "min_interval_sec": 1800, "day_gap_hours": 4, "work_hours": 9,
        "max_auto_deletes": 20, "max_auto_delete_mb": 200}

# 처음 두 번은 틱 간격, 그다음부터 30분 x2 · x4 · x8 · x8(최대) — 분 단위 간격
BACKOFF_GAPS = [2, 2, 60, 120, 240]


def _fail(fault: str, **extra) -> dict:
    return {"outcome": "failed", "fault": fault, **extra}


FAILURES = {
    "auth": (_fail("auth"), 1),
    "service_error": (_fail("service_error"), 1),
    "service_down": (_fail("service_down"), 1),
    "network_offline": (_fail("network_offline"), 1),
    "network_unstable": (_fail("network_unstable"), 1),
    "local": (_fail("local"), 1),
    "unknown": (_fail("unknown"), 1),
    "local_collapse": ({"outcome": "held", "fault": "held", "held_reason": "local_collapse"}, 4),
}


class _Loop:
    """decide·_apply_backoff·_write_status 를 그대로 두는 틱 환경. 자식 실행과 상태 DB 만
    흉내 낸다 — 성공한 실행만 last_sync_at 을 남긴다(실제 CLI 와 같다)."""

    def __init__(self, monkeypatch, *, names=("A", "W"), synced=None, disk=False) -> None:
        if not disk:
            # 틱마다 state.json(fsync)·status.txt 를 쓰면 수백 틱이 분 단위가 된다. 판정에는
            # 닿지 않는다(st 는 메모리에서 이어진다) — 디스크를 보는 시험만 disk=True.
            monkeypatch.setattr(runner, "_write_status", lambda *a, **k: None)
            monkeypatch.setattr(AutoState, "save", lambda self: None)
        self.clock = NOW
        self.names = list(names)
        self.synced: dict[str, dt.datetime | None] = {
            "A": NOW - dt.timedelta(minutes=10), "W": STALE, **(synced or {})}
        self.fail: dict[str, tuple[dict, int]] = {}      # 이름 → (보고, 종료코드). 없으면 성공
        self.runs: list[tuple[str, dt.datetime]] = []
        self.last_report: dict[str, dict] = {}
        monkeypatch.setattr(runner, "load_auto", lambda: dict(AUTO))
        monkeypatch.setattr(runner, "auto_profiles", lambda only=None: list(self.names))
        monkeypatch.setattr(runner, "_reconcile", lambda st, names: None)
        monkeypatch.setattr(keys, "drain", lambda backend=None: "")
        monkeypatch.setattr(runner, "_now", lambda: self.clock)
        monkeypatch.setattr(runner, "_last_sync_at", lambda name: self.synced.get(name))
        monkeypatch.setattr(runner, "_last_sync_map",
                            lambda names: {n: self.synced.get(n) for n in names})
        monkeypatch.setattr(runner, "_run_child", self._child)
        monkeypatch.setattr(runner, "_read_report",
                            lambda name: dict(self.last_report.get(name, {})))
        self.st = AutoState({"last_tick": (NOW - TICK).isoformat(),
                             "day_start": (NOW - dt.timedelta(hours=1)).isoformat(),
                             "last_eod_date": NOW.date().isoformat()})   # 퇴근 스윕은 이 시험 밖

    def _child(self, name: str, auto: dict, extra: list[str]) -> int:
        self.runs.append((name, self.clock))
        if name in self.fail:
            report, rc = self.fail[name]
            self.last_report[name] = dict(report)
            return rc
        self.synced[name] = self.clock
        self.last_report[name] = {"outcome": "ok", "fault": "ok"}
        return 0

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            runner._tick(self.st, [], None)
            runner._end_idle()
            self.clock += TICK

    def run_for(self, hours: float) -> None:
        self.tick(int(hours * 30))

    def times(self, name: str) -> list[dt.datetime]:
        return [t for n, t in self.runs if n == name]

    def gaps(self, name: str) -> list[int]:
        t = self.times(name)
        return [int((b - a).total_seconds() // 60) for a, b in zip(t, t[1:])]


# ==========================================================================
# 1) 연속 실패는 마지막 시도부터 센다 — 원인과 마지막 성공 시각에 관계없이
# ==========================================================================
@pytest.mark.parametrize("fault, last_ok", [
    *[(f, "stale") for f in sorted(FAILURES)],
    ("network_offline", "due_now"), ("auth", "never"),
])
def test_repeated_failure_backs_off_from_the_last_attempt(monkeypatch, fault, last_ok):
    """8시간 동안 실패가 이어져도 그 폴더는 6번만 돈다. 예전에는 마지막 성공이 4시간보다
    오래되는 순간부터 틱마다 돌았다(닷새 전 성공이면 8시간에 240번, 방금 밀린 폴더도
    3시간 반 뒤부터). 한 번도 성공한 적 없는 폴더는 처음부터 틱마다였다."""
    seen = {"stale": STALE, "due_now": NOW - dt.timedelta(minutes=30), "never": None}[last_ok]
    loop = _Loop(monkeypatch, synced={"W": seen})
    loop.fail["W"] = FAILURES[fault]
    loop.run_for(8)
    assert loop.times("W")[0] == NOW
    assert loop.gaps("W") == BACKOFF_GAPS, loop.gaps("W")
    assert loop.st.fail_streak("W") == 6 and loop.st.backoff_mult("W") == 8.0
    # 다른 폴더는 평시 주기대로 — 실패하는 폴더가 차례를 빼앗지 않는다
    assert all(g == 30 for g in loop.gaps("A")), loop.gaps("A")
    assert len(loop.times("A")) >= 15


def test_rate_limited_failure_reaches_the_cap_sooner(monkeypatch):
    """429 는 실패마다 즉시 2배 — 세 번째 실패에 이미 x8 이고, 그때부터 마지막 시도부터 센다."""
    loop = _Loop(monkeypatch)
    loop.fail["W"] = (_fail("rate_limited", rate={"rate_limited": 3}), 1)
    loop.run_for(9)
    assert loop.gaps("W") == [2, 2, 240, 240], loop.gaps("W")


# ==========================================================================
# 2) 잠깐의 끊김은 틱 간격으로 회복한다
# ==========================================================================
def test_short_blip_recovers_within_minutes(monkeypatch):
    loop = _Loop(monkeypatch, names=("W",), synced={"W": NOW - dt.timedelta(minutes=30)})
    loop.fail["W"] = FAILURES["network_offline"]
    loop.tick(2)                                   # 10:56, 10:58 실패
    del loop.fail["W"]                             # 연결이 돌아왔다
    loop.run_for(2)
    assert loop.times("W")[:3] == [NOW, NOW + TICK, NOW + 2 * TICK]   # 세 번째(4분 뒤)에 성공
    assert loop.st.fail_streak("W") == 0 and loop.st.backoff_mult("W") == 1.0
    assert loop.gaps("W") == [2, 2, 30, 30, 30], "회복 뒤에 평시 주기로 돌아가지 않았다"


def test_recovery_after_a_long_outage_counts_from_the_success_again(monkeypatch):
    """긴 장애가 끝나면 다음 예정 시도가 성공하고, 그 뒤로는 다시 마지막 동기화부터 센다
    (승수는 성공할 때마다 x0.75 로 서서히 — 기존 설계 그대로)."""
    loop = _Loop(monkeypatch, names=("W",))
    loop.fail["W"] = FAILURES["service_down"]
    loop.run_for(4)                                # 10:56·10:58·11:00·12:00·14:00 실패
    assert loop.gaps("W") == [2, 2, 60, 120]
    del loop.fail["W"]
    loop.run_for(4.5)                              # 18:00 에 다음 시도 — 성공
    ok_at = NOW.replace(hour=18, minute=0)
    assert loop.times("W")[5] == ok_at and loop.synced["W"] == ok_at
    assert loop.st.fail_streak("W") == 0 and loop.st.backoff_mult("W") == 6.0
    assert loop.times("W")[5:] == [ok_at], "성공 뒤에 틱마다 돌았다"   # 다음은 18:00 + 30분 x6


# ==========================================================================
# 3) 화면·대기 줄·status.txt 가 decide 와 같은 시각을 말한다
# ==========================================================================
_STREAK_LINE = re.compile(r"\(연속 (\d+)회 같은 결과 — (.+?) · 다음 시도 (\d\d:\d\d)\)")


def test_streak_line_names_the_time_decide_actually_uses(monkeypatch, capsys):
    loop = _Loop(monkeypatch)
    loop.fail["W"] = FAILURES["auth"]
    promised: list[tuple[int, str, str]] = []
    for _ in range(8 * 30):
        loop.tick()
        promised += [(int(n), grew, at) for n, grew, at in _STREAK_LINE.findall(
            capsys.readouterr().out)]
    assert [(n, grew) for n, grew, _ in promised] == [
        (3, "주기를 2배로 늘립니다"), (4, "주기를 4배로 늘립니다"),
        (5, "주기를 8배로 늘립니다"), (6, "주기 8배(최대)")]
    actual = [f"{t:%H:%M}" for t in loop.times("W")[3:]]            # 각 줄 다음의 실제 실행
    assert [at for *_, at in promised][:len(actual)] == actual
    assert [at for *_, at in promised][-1] == "22:00"                # 18:00 + 30분 x8


def test_first_two_failures_print_no_backoff_line(monkeypatch, capsys):
    loop = _Loop(monkeypatch, names=("W",))
    loop.fail["W"] = FAILURES["network_offline"]
    loop.tick(2)
    assert "연속" not in capsys.readouterr().out


def test_idle_line_and_status_txt_name_the_next_attempt(monkeypatch, capsys):
    loop = _Loop(monkeypatch, names=("W",), disk=True)
    loop.fail["W"] = FAILURES["auth"]
    loop.tick(3)                                   # 10:56·10:58·11:00 — 세 번째에 x2
    capsys.readouterr()
    loop.tick()                                    # 11:02 대기
    assert "대기 중  다음 동기화 12:00" in capsys.readouterr().out
    status = Path(ext_path(auto_dir() / "status.txt")).read_text(encoding="utf-8")
    line = next(s for s in status.splitlines() if s.strip().startswith("W "))
    assert "[연속 실패 3회 · 주기 x2 · 다음 시도 12:00]" in line, line
    assert "마지막 11-05 09:12" in line            # '마지막'은 마지막 성공 그대로
    loop.run_for(1)
    assert loop.times("W")[3] == NOW.replace(hour=12, minute=0)


def test_status_tag_after_recovery_points_at_the_last_sync(monkeypatch):
    """성공 뒤 승수가 남아 있는 동안은 '연속 실패' 없이 주기와 다음 시각만."""
    st = AutoState({})
    st.set_backoff_mult("W", 6.0)
    tag = runner.backoff_tag(st, "W", NOW, 1800, NOW + dt.timedelta(minutes=1))
    assert tag == "  [주기 x6 · 다음 시도 13:56]"
    assert runner.backoff_tag(AutoState({}), "W", NOW, 1800, NOW) == ""


def test_auto_status_command_uses_the_same_tag(monkeypatch):
    real_now = dt.datetime.now().replace(microsecond=0)
    st = AutoState({"profiles": {"W": {
        "fail_streak": 3, "backoff_mult": 2.0,
        "last_attempt": (real_now - dt.timedelta(minutes=1)).isoformat()}}})
    monkeypatch.setattr(runner, "_last_sync_at", lambda name: STALE)
    tag = auto_cli._backoff_tag(st, "W", 1800)
    assert tag.startswith("  [연속 실패 3회 · 주기 x2 · 다음 시도 "), tag


# ==========================================================================
# 4) 시도 시각은 state.json 에 남는다 — 창을 다시 띄워도 이어진다
# ==========================================================================
def test_backoff_survives_a_window_restart(monkeypatch):
    loop = _Loop(monkeypatch, names=("W",), disk=True)
    loop.fail["W"] = FAILURES["network_offline"]
    loop.tick(3)
    assert Path(ext_path(state_path())).exists()
    reopened = AutoState()                         # 디스크에서 다시 읽는다(격리된 상태 폴더)
    assert reopened.last_attempt("W") == "2026-11-10T11:00:00"
    assert reopened.fail_streak("W") == 3
    loop.st = reopened
    loop.run_for(1)
    assert loop.times("W")[3:] == [NOW.replace(hour=12, minute=0)]


def test_locked_is_not_an_attempt(monkeypatch):
    """다른 실행과 겹친 것은 시도가 아니다 — '다음 틱에' 다시 해야 하므로 시도 시각을
    남기지 않는다."""
    st = AutoState({"profiles": {"W": {"fail_streak": 5, "backoff_mult": 8.0,
                                       "last_attempt": "2026-11-10T09:00:00"}}})
    runner._apply_backoff(st, "W", "locked", {}, at=NOW, interval=1800, seen=STALE)
    assert st.last_attempt("W") == "2026-11-10T09:00:00"
    assert runner._due_at(st, "W", STALE, 1800, NOW) <= NOW


def test_ok_run_records_the_attempt_but_counts_from_the_sync(monkeypatch):
    st = AutoState({"profiles": {"W": {"fail_streak": 4, "backoff_mult": 4.0}}})
    runner._apply_backoff(st, "W", "ok", {}, at=NOW, interval=1800, seen=NOW)
    assert st.last_attempt("W") == NOW.isoformat(timespec="seconds")
    assert st.fail_streak("W") == 0 and st.backoff_mult("W") == 3.0
    assert runner._due_at(st, "W", NOW, 1800, NOW) == NOW + dt.timedelta(minutes=90)


# ==========================================================================
# 5) 설정 오류로 쉬는 폴더는 다른 폴더의 차례를 빼앗지 않는다
# ==========================================================================
CONFIG = ({"outcome": "failed", "fault": "config"}, 2)


def _write_config(text: str) -> None:
    """쉬는 상태를 정하는 설정 지문의 근거(격리된 설정 폴더). 대상 목록은 _Loop 이 따로
    정하므로 내용은 상관없다 — 바뀌었는가만 본다."""
    path = Path(ext_path(config_path()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_config_resting_profile_does_not_starve_the_others(monkeypatch, capsys):
    """쉬는 폴더는 마지막 성공이 가장 오래돼 언제나 '가장 오래 밀린' 자리에 있다. 그것을
    고르고 건너뛰면 그 틱이 통째로 빈다 — 예전에는 3시간 동안 다른 폴더가 0회 돌고
    '설정 문제로 쉬는 중'이 89줄 찍혔다(2026-11-11 재현)."""
    _write_config("# v1\n")
    loop = _Loop(monkeypatch)
    loop.fail["W"] = CONFIG
    loop.run_for(3)
    assert loop.times("W") == [NOW]                          # 한 번 거부되고 쉰다
    assert loop.times("A")[0] == NOW + dt.timedelta(minutes=20)
    assert loop.gaps("A") == [30] * 5, loop.gaps("A")        # 평시 주기 그대로
    assert "설정 문제로 쉬는 중" not in capsys.readouterr().out   # 스윕이 없으면 한 줄도


def test_config_change_resumes_the_resting_profile_at_once(monkeypatch):
    _write_config("# v1\n")
    loop = _Loop(monkeypatch, names=("W",))
    loop.fail["W"] = CONFIG
    loop.tick(5)
    assert loop.times("W") == [NOW]
    _write_config("# v2 — 고쳤다\n")
    del loop.fail["W"]
    loop.tick()
    assert loop.times("W") == [NOW, NOW + 5 * TICK]
    assert loop.synced["W"] == NOW + 5 * TICK


def test_idle_line_does_not_promise_the_resting_profile(monkeypatch, capsys):
    """쉬는 폴더의 차례는 이미 지난 시각이다 — 넣으면 대기 줄이 오지 않을 동기화를 적는다."""
    _write_config("# v1\n")
    loop = _Loop(monkeypatch)
    loop.fail["W"] = CONFIG
    loop.tick()                                              # 10:56 W 거부
    capsys.readouterr()
    loop.tick()                                              # 10:58 대기
    assert "대기 중  다음 동기화 11:16" in capsys.readouterr().out   # A 의 차례


def test_idle_line_when_every_profile_rests(monkeypatch, capsys):
    _write_config("# v1\n")
    loop = _Loop(monkeypatch, names=("W",))
    loop.fail["W"] = CONFIG
    loop.tick()
    capsys.readouterr()
    loop.tick()
    out = capsys.readouterr().out
    assert "대기 중  다음 동기화 -" in out and "가장 오래 밀린" not in out
