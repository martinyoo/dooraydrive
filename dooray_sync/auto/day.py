"""자동 루프의 '근무일' — 기동 시각과 퇴근 스윕을 정하는 단일 정본.

판정(`runner.decide`)과 표시 셋(창 머리글·status.txt·`--auto status`)이 **같은
함수**를 본다. 표시가 판정과 따로 계산하면 화면이 판정과 다른 말을 한다.

**근무일의 정의**

- 기동(`day_start`)은 직전 틱과 `day_gap_hours` 이상 벌어진 뒤의 첫 틱에만 새로
  정해진다(부팅·절전 복귀). 밤새 켜 둔 PC는 2분마다 틱이 돌아 그런 틱이 없다 —
  **자정은 기동이 아니다.** 설계 의도다: 자정 틱을 기동으로 읽으면 그날 퇴근 스윕이
  00:01 + 근무시간 - 15분, 즉 아침 08:46이 된다.
- 그래서 근무일은 달력의 자정이 아니라 **마지막 기동 시각에서 24시간씩** 넘어간다.
  11-10 07:08에 기동한 PC가 계속 켜져 있으면 11-11의 근무일은 07:08에 시작한
  것으로 **가정**하고(표시에 그렇게 밝힌다) 그날 퇴근 스윕은 15:53이다. 밤에 켠
  PC(16:00 기동 → 퇴근 스윕 다음 날 00:45)도 같은 규칙으로 자기 스윕을 지킨다.
- 퇴근 스윕은 근무일마다 1회다. 마쳤는지는 **그 근무일의 퇴근 스윕 예정 시각이
  속한 날짜**(`last_eod_date`)로 본다. 실행한 순간의 날짜가 아니다.
- 기동(출근 스윕)은 넘어가는 근무일에 새로 돌지 않는다 — 밤새 켜진 PC는 30분
  평시가 계속 돌았으므로 '돌아와서 한 바퀴'가 필요 없다.

2026-11-11 실측(개발 PC): 마쳤는지를 '지금'의 날짜로 판정했더니, 밤새 켜 둔 PC에서
자정을 넘는 순간 어제(11-10 07:08 기동)의 15:53 스윕이 '오늘 아직 안 한 것'으로
보여 00:01에 전체 스윕이 돌았다. 그 스윕이 오늘 날짜를 기록해 **오늘의 진짜 퇴근
스윕은 사라졌고**, 창 머리글·status.txt·`--auto status`는 어제의 15:53을 오늘
것처럼 '(완료)'로 보였다. 표시 셋이 각자 `day_start + 근무시간`을 계산하고 있어서
판정이 고쳐져도 화면은 그대로 거짓말을 했을 것이다 — 그래서 여기 한 곳에 모은다.
"""
from __future__ import annotations

import datetime as _dt
from typing import NamedTuple

from .state import AutoState

__all__ = ["EOD_LEAD", "Day", "parse_iso", "starts_new_day", "current_day",
           "summary"]

EOD_LEAD = _dt.timedelta(minutes=15)     # 퇴근 몇 분 전에 스윕할까
_DAY = _dt.timedelta(days=1)


def parse_iso(iso: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def starts_new_day(st: AutoState, auto: dict, now: _dt.datetime) -> bool:
    """이번 틱이 기동(출근 스윕)인가 — 직전 틱과 day_gap_hours 이상 벌어졌다.

    day_start가 없거나 못 읽어도 기동으로 친다. 없으면 퇴근 스윕의 기준점이 없어
    **그날 퇴근 스윕이 영영 안 뜬다** — 상태 파일이 지워졌거나 깨진 경우가 실제로
    그렇게 된다. 자기 치유 경로.
    """
    prev = parse_iso(st.last_tick)
    if prev is None or parse_iso(st.day_start) is None:
        return True
    return now - prev >= _dt.timedelta(hours=float(auto["day_gap_hours"]))


class Day(NamedTuple):
    """지금이 속한 근무일."""

    since: _dt.datetime   # 실측 기동(state의 day_start)
    start: _dt.datetime   # 이 근무일의 기동 — since 이거나, since에서 24시간씩 넘긴 가정
    eod: _dt.datetime     # 이 근무일의 퇴근 스윕 예정 시각
    done: bool            # 이 근무일의 퇴근 스윕을 이미 마쳤다

    @property
    def carried(self) -> bool:
        """실측 기동이 아니라 이어 붙인 가정인가(밤새 켜 둔 PC)."""
        return self.start != self.since

    @property
    def weekend(self) -> bool:
        """퇴근 스윕 시각이 토·일이면 스윕 없음 — 출근 스윕과 30분 평시만(사용자 결정)."""
        return self.eod.weekday() >= 5

    @property
    def key(self) -> str:
        """퇴근 스윕을 마쳤을 때 last_eod_date에 남길 값."""
        return self.eod.date().isoformat()

    def pending(self, now: _dt.datetime) -> bool:
        """오늘 퇴근 스윕이 아직 시각 전이다(대기 줄에 보일 것)."""
        return not self.weekend and not self.done and now < self.eod

    def due(self, now: _dt.datetime) -> bool:
        """지금 퇴근 스윕을 돌릴 차례다."""
        return not self.weekend and not self.done and now >= self.eod


def current_day(st: AutoState, auto: dict, now: _dt.datetime) -> Day | None:
    """지금이 속한 근무일. day_start를 못 읽으면 None(다음 틱이 기동이다)."""
    since = parse_iso(st.day_start)
    if since is None:
        return None
    # 시계가 뒤로 간 경우(since > now)는 넘기지 않는다 — 음수 일수로 '어제'를
    # 만들면 이미 지나간 퇴근 스윕이 생겨 즉시 돈다.
    days = max(0, (now - since) // _DAY)
    start = since + days * _DAY
    eod = start + _dt.timedelta(hours=float(auto["work_hours"])) - EOD_LEAD
    return Day(since, start, eod, st.last_eod_date == eod.date().isoformat())


def _hm(when: _dt.datetime, now: _dt.datetime) -> str:
    """오늘이면 HH:MM, 다른 날이면 MM-DD HH:MM — 어제의 시각이 오늘 것처럼 읽히지 않게."""
    return f"{when:%H:%M}" if when.date() == now.date() else f"{when:%m-%d %H:%M}"


def summary(st: AutoState, auto: dict, now: _dt.datetime, *,
            tick_now: bool = False) -> tuple[str, str]:
    """(기동, 퇴근 스윕) 표시 문구 — decide가 쓰는 것과 같은 계산으로 만든다.

    tick_now: 곧바로 틱이 돈다(창 머리글). 그 틱이 기동이면 지금을 기동으로 놓고
    미리 보인다. 아니면(`--auto status`·status.txt) 다음 틱이 언제일지 모르므로
    '다음 틱에서 정함'으로만 적는다 — 닫힌 창이 남긴 옛 시각을 오늘 것처럼 보이지
    않는다.
    """
    if starts_new_day(st, auto, now):
        if tick_now:
            eod = now + _dt.timedelta(hours=float(auto["work_hours"])) - EOD_LEAD
            text = f"{eod:%H:%M} 예정(첫 틱에 확정)"
            if eod.weekday() >= 5:
                text += " — 주말이라 건너뜀"
            return f"{now:%H:%M} (첫 틱이 기동)", text
        start = "다음 틱에서 새로 정함"
        last = parse_iso(st.day_start)
        if last is not None:
            start += f" (지난 기동 {last:%m-%d %H:%M})"
        return start, "다음 기동 때 정해짐"

    day = current_day(st, auto, now)
    if day is None:           # starts_new_day가 아니면 오지 않는다 — 방어만
        return "-", "-"
    start = _hm(day.start, now)
    if day.carried:
        start += f" ({day.since:%m-%d %H:%M} 기동 후 계속 켜짐 — 같은 시각으로 가정)"
    if day.weekend:
        state = "(주말이라 건너뜀)"
    elif day.done:
        state = "(완료)"
    elif now >= day.eod:
        state = "(다음 틱에 실행)"
    else:
        state = "(예정)"
    return start, f"{_hm(day.eod, now)} {state}"
