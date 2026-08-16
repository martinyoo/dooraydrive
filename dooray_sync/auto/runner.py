"""자동 동기화 루프 — 창 안에서 도는 본체.

**판정은 여기서, 실행은 CLI가.** 러너는 "지금 무엇을 돌릴까"만 정하고 자식
프로세스(`dsync sync`)를 띄운다. 안전 판정(삭제 등급화·충돌 강등·로컬 붕괴
보류)은 전부 CLI 정본 안에 있고 러너는 그 인자를 **항상** 붙일 뿐이다 —
러너에만 있는 안전장치를 만들면 사람 경로와 무인 경로의 판정이 갈라진다.

틱 판정(설계):
  1. 출근 스윕 — 직전 틱과 day_gap_hours 이상 벌어진 뒤의 첫 틱
     (부팅·절전 복귀를 함께 잡고, 밤샘 가동 PC의 자정 틱 오인을 막는다)
  2. 퇴근 스윕 — 평일이고 지금 >= 기동 + work_hours - 15분, 하루 1회
  3. 평시     — 프로파일당 min_interval_sec 간격, 틱당 가장 오래 밀린 1개
"""
from __future__ import annotations

import datetime as _dt
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .. import __version__
from ..config import config_path, db_path, load_auto
from ..util.paths import ext_path
from . import notify
from .state import AutoState, auto_dir

__all__ = ["run_loop", "run_once", "decide", "Decision"]

EOD_LEAD = _dt.timedelta(minutes=15)     # 퇴근 몇 분 전에 스윕할까
_REPO = Path(__file__).resolve().parent.parent.parent


class Decision:
    """이번 틱에 무엇을 할지. (kind, names, why)"""

    def __init__(self, kind: str, names: list[str], why: str = "") -> None:
        self.kind = kind          # 'sweep_start' | 'sweep_eod' | 'due' | 'idle'
        self.names = names
        self.why = why

    def __repr__(self) -> str:    # 테스트 실패 메시지를 읽을 수 있게
        return f"Decision({self.kind!r}, {self.names!r}, {self.why!r})"


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def _parse(iso: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def auto_profiles(only: list[str] | None = None) -> list[str]:
    """자동 대상 프로파일 이름(이름순). only가 있으면 그 교집합.

    조건은 `auto_sync=true` **AND** `sync_mode='sync'`다 — 두 값은 직교한다
    (sync_mode='무엇을 하는가', auto_sync='누가 시키는가'). 마커 정합이
    sync_mode를 껐는데 여기서 안 보면 매 틱 자식이 exit 2로 거부당하고,
    그 거부가 로그를 채워 진짜 신호를 묻는다.

    이름순 고정이 중요하다 — config 파일 순서에 실행 순서를 묶으면 문서상
    마지막 프로파일이 상습적으로 굶는다.
    """
    import tomllib
    try:
        with open(ext_path(config_path()), "rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError):
        return []
    names = [
        name for name, body in (doc.get("profile") or {}).items()
        if isinstance(body, dict) and body.get("auto_sync") is True
        and str(body.get("sync_mode") or "sync").strip().lower() == "sync"
    ]
    if only is not None:
        allow = {n.casefold() for n in only}
        names = [n for n in names if n.casefold() in allow]
    return sorted(names, key=str.casefold)


def _ro_uri(path: Path) -> str:
    r"""읽기 전용 SQLite URI.

    **ext_path를 쓰면 안 된다.** 확장 길이 접두(\\?\)를 URI에 넣으면 '//?/…'가
    되고, 그 '?'를 퍼센트 인코딩하면 authority가 '%3f'로 잡혀
    `invalid uri authority`로 연결 자체가 실패한다. 2026-08-16 실측: 그 실패를
    None(한 번도 동기화 안 함)으로 읽어 30분 주기가 통째로 무력해졌고 매 틱
    동기화가 돌았다. URI에는 평범한 경로를 쓰고 '?'·'#'만 인코딩한다.
    """
    text = str(path).replace("\\", "/").replace("?", "%3F").replace("#", "%23")
    return f"file:{text}?mode=ro"


def _last_sync_at(name: str) -> _dt.datetime | None:
    """상태 DB의 마지막 sync 시각. **프로세스를 띄우지 않고** 읽기 전용으로 연다.

    일반 connect는 DB가 없을 때 빈 파일을 만든다 — 그러면 편입 게이트의
    '상태 DB 없음' 판정이 다음부터 통과해 버린다. mode=ro로 막는다.

    읽기에 실패하면 **None이 아니라 예외**를 올린다. None은 '한 번도 동기화한
    적 없음'이라는 뜻이고, 그건 '즉시 실행'으로 이어진다 — 조회 실패를 그렇게
    읽으면 고장이 폭주로 바뀐다(위 실측 사고).
    """
    import sqlite3
    path = db_path(name)
    if not os.path.exists(ext_path(path)):
        return None
    conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=2.0)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_sync_at'").fetchone()
    finally:
        conn.close()
    return _parse(row[0]) if row and row[0] else None


def _last_sync_map(names: list[str]) -> dict[str, _dt.datetime | None]:
    """프로파일별 마지막 sync 시각. 조회가 실패한 프로파일은 **이번 틱에서
    제외**한다 — 모른다는 이유로 실행하면 고장이 곧 폭주다(fail-closed)."""
    out: dict[str, _dt.datetime | None] = {}
    for name in names:
        try:
            out[name] = _last_sync_at(name)
        except Exception as exc:      # noqa: BLE001 — sqlite·OS 오류 전부
            print(f"    ({name}: 마지막 동기화 시각을 읽지 못해 이번 틱은 "
                  f"건너뜁니다 — {type(exc).__name__}: {exc})")
    return out


def decide(st: AutoState, auto: dict, names: list[str], *, now: _dt.datetime,
           last_sync: dict[str, _dt.datetime | None]) -> Decision:
    """순수 판정 — 시계·상태·설정만 보고 무엇을 돌릴지 정한다(부작용 없음)."""
    if not names:
        return Decision("idle", [], "자동 대상 프로파일이 없습니다")

    prev = _parse(st.last_tick)
    start = _parse(st.day_start)
    gap = _dt.timedelta(hours=float(auto["day_gap_hours"]))
    # day_start가 없으면 무조건 기동으로 친다. 없으면 퇴근 스윕의 기준점이 없어
    # **그날 퇴근 스윕이 영영 안 뜬다** — 상태 파일이 지워졌거나 --auto now가
    # last_tick만 남긴 경우가 실제로 그렇게 된다. 자기 치유 경로.
    if prev is None or start is None or now - prev >= gap:
        return Decision("sweep_start", list(names),
                        "기동(첫 틱 또는 간격이 벌어짐) — 전체 한 바퀴")

    if (start is not None and now.weekday() < 5
            and st.last_eod_date != now.date().isoformat()):
        eod = start + _dt.timedelta(hours=float(auto["work_hours"])) - EOD_LEAD
        if now >= eod:
            return Decision("sweep_eod", list(names),
                            f"퇴근 스윕({eod:%H:%M}) — 전체 한 바퀴")

    interval = float(auto["min_interval_sec"])
    due: list[tuple[float, str]] = []
    for name in names:
        seen = last_sync.get(name)
        if seen is None:
            due.append((0.0, name))
            continue
        wait = interval * st.backoff_mult(name)
        if (now - seen).total_seconds() >= wait:
            due.append((seen.timestamp(), name))
    if not due:
        return Decision("idle", [], "")
    due.sort()
    return Decision("due", [due[0][1]], "가장 오래 밀린 프로파일")


def _child_cmd(name: str, auto: dict, extra: list[str]) -> list[str]:
    """자식 명령줄(정본). 무인 인자는 **항상** 붙는다 — 붙이지 않는 경로를
    만들지 않으므로 우회가 구조적으로 불가능하다."""
    report = auto_dir() / "last" / f"{name}.json"
    return [
        sys.executable, "-P", "-m", "dooray_sync.cli.main", "sync", "-p", name,
        "--unattended",
        "--max-auto-deletes", str(auto["max_auto_deletes"]),
        "--max-auto-delete-mb", str(auto["max_auto_delete_mb"]),
        "--report-json", str(report),
        *extra,
    ]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_REPO) + (os.pathsep + prev if prev else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _neutral_cwd() -> str:
    """자식의 작업 디렉터리 — **프로그램 폴더가 아니어야 한다**(I-A8).
    Windows에서 어떤 프로세스의 CWD인 폴더는 rename이 안 돼, 설치.bat의
    폴더 통째 교체가 동기화 도중이면 막힌다."""
    target = auto_dir()
    try:
        os.makedirs(ext_path(target), exist_ok=True)
        return str(target)
    except OSError:
        return str(Path.home())


def _run_child(name: str, auto: dict, extra: list[str]) -> int:
    cmd = _child_cmd(name, auto, extra)
    try:
        return subprocess.call(cmd, cwd=_neutral_cwd(), env=_child_env(),
                               stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f"    [오류] 자식 실행 실패 — {type(exc).__name__}: {exc}")
        return 1


def _classify(rc: int, report: dict) -> tuple[str, str]:
    """(fault, 사람이 읽는 말). **종료코드가 아니라 보고의 fault가 정본이다** —
    exit 1 하나에 와이파이 끊김·Dooray 장애·일부 파일 실패가 전부 들어 있어
    구분이 안 된다. 셋은 사람이 할 일이 완전히 다르다.

    exit 1이 곧 고장은 아니다(I-A11). 보고가 없으면(자식이 쓰기 전에 죽었으면)
    종료코드로 떨어뜨린다.
    """
    from ..api.faults import ADVICE, LABEL, Fault

    fault = str(report.get("fault") or "")
    if not fault:
        fault = {
            0: Fault.OK, 1: Fault.PARTIAL, 2: Fault.CONFIG,
            3: "locked", 4: Fault.HELD,
        }.get(rc, Fault.UNKNOWN)
    if fault == "locked":
        return "locked", "다른 실행이 사용 중 — 다음 틱에"
    label = LABEL.get(fault, fault)
    advice = ADVICE.get(fault, "")
    return fault, f"{label}" + (f" — {advice}" if advice else "")


def _read_report(name: str) -> dict:
    """자식이 남긴 실행 보고. 없거나 깨졌으면 빈 dict — 보고가 없다는 것 자체가
    '자식이 보고를 쓰기 전에 죽었다'는 신호라서 호출측이 그렇게 다룬다."""
    import json
    try:
        with open(ext_path(auto_dir() / "last" / f"{name}.json"), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _apply_backoff(st: AutoState, name: str, kind: str, report: dict) -> None:
    """주기 승수 조정. 두 가지 입력이 있다.

    - rate limit 관측(429): 서버가 밀어내고 있다 → 즉시 2배.
    - 연속 실패: 원인이 무엇이든 같은 실패를 2분마다 반복하는 것은 서버에도
      로그에도 해롭다 → 3회 연속부터 2배씩.
    깨끗한 실행은 서서히 되돌린다(x0.75, 하한 1.0).
    """
    rate = report.get("rate") or {}
    try:
        limited = int(rate.get("rate_limited") or 0)
    except (TypeError, ValueError):
        limited = 0

    from ..api.faults import Fault

    entry = st.profile(name)
    streak = int(entry.get("fail_streak") or 0)
    # OK가 아닌 모든 결과가 연속 회수를 올린다 — 원인이 무엇이든 같은 결과를
    # 2분마다 반복하는 것은 서버에도 로그에도 해롭다. locked는 정상 경합이라 뺀다.
    streak = 0 if kind in (Fault.OK, "locked") else streak + 1
    entry["fail_streak"] = streak

    cur = st.backoff_mult(name)
    if limited > 0:
        st.set_backoff_mult(name, cur * 2)
        print(f"    (rate limit {limited}회 관측 — 이 폴더 주기를 "
              f"{st.backoff_mult(name):g}배로 늘립니다)")
    elif streak >= 3:
        st.set_backoff_mult(name, cur * 2)
        print(f"    (연속 {streak}회 같은 결과 — 주기를 "
              f"{st.backoff_mult(name):g}배로 늘립니다)")
    elif kind == "ok" and cur > 1.0:
        st.set_backoff_mult(name, max(1.0, cur * 0.75))


def _config_mtime() -> float:
    try:
        return os.path.getmtime(ext_path(config_path()))
    except OSError:
        return 0.0


def _note_outcome(st: AutoState, name: str, kind: str, msg: str,
                  report: dict, now: _dt.datetime) -> None:
    """실행 결과를 통지로 옮긴다. 원인이 해소되면 스스로 지워진다."""
    ts = now.isoformat(timespec="seconds")
    entry = st.profile(name)

    if kind == "config":
        # 같은 거부를 2분마다 영원히 반복하지 않는다. config가 바뀌거나 사람이
        # 그 폴더에서 실행하기 전까지 이 프로파일의 자동 실행을 쉰다.
        entry["config_error_mtime"] = _config_mtime()
        notify.add(name, "config", "설정 문제로 자동 실행을 멈췄습니다 - "
                                   "그 폴더에서 synchere.bat을 한 번 실행하세요", ts=ts)
        return
    entry.pop("config_error_mtime", None)

    from ..api.faults import ADVICE, LABEL, Fault, is_transient

    if kind == Fault.HELD:
        why = str(report.get("held_reason") or "")
        detail = {"local_collapse": "로컬 폴더가 비어 보입니다(드라이브·백신 확인)"}.get(
            why, "무인 실행이 보류했습니다")
        notify.add(name, "held", f"{detail} - 폴더에서 직접 실행해 확인하세요", ts=ts)
        return
    notify.clear(name, "held")

    if kind in (Fault.OK, Fault.PARTIAL):
        # 일부 파일 실패는 정상 운용에서 늘 생긴다 — 사람을 부르지 않는다(I-A11).
        notify.clear(name, "error")
    elif is_transient(kind):
        # 와이파이 끊김·Dooray 장애·한도 초과는 기다리면 풀린다. 사람을 부르는
        # 대신 연속 실패 회수를 세고, 오래 이어질 때만 알린다 — 잠깐 끊길 때마다
        # 알림이 뜨면 알림 자체가 무시된다.
        streak = int(entry.get("fail_streak") or 0)
        if streak >= 5:
            notify.add(name, "error",
                       f"{LABEL.get(kind, kind)}이(가) {streak}회 이어집니다 - "
                       f"{ADVICE.get(kind, '')}", ts=ts)
        else:
            notify.clear(name, "error")
        return
    else:
        # auth·service_error·local·unknown — 기다려도 안 풀린다. 즉시 알린다.
        notify.add(name, "error",
                   f"{LABEL.get(kind, kind)} - {ADVICE.get(kind, msg)}", ts=ts)
        return

    deletes = report.get("deletes") or {}
    deferred = int(deletes.get("deferred") or 0)
    if deferred:
        notify.add(name, "deletes",
                   f"삭제 {deferred}건이 임계를 넘어 보류됐습니다 - "
                   f"폴더에서 직접 실행하면 계획을 보고 처리할 수 있습니다", ts=ts)
    else:
        notify.clear(name, "deletes")

    conflicts = int(report.get("conflicts_deferred") or 0)
    if conflicts:
        notify.add(name, "conflicts",
                   f"충돌 {conflicts}건이 대기 중입니다 - 무인 실행은 로컬 원본을 "
                   f"개명하지 않습니다", ts=ts)
    else:
        notify.clear(name, "conflicts")


def _skipped_by_config_error(st: AutoState, name: str) -> bool:
    """설정 오류로 쉬는 중인가. config 파일이 바뀌면 자동으로 풀린다."""
    stamp = st.profile(name).get("config_error_mtime")
    if stamp is None:
        return False
    try:
        return float(stamp) == _config_mtime()
    except (TypeError, ValueError):
        return False


def _write_status(st: AutoState, auto: dict, names: list[str],
                  now: _dt.datetime) -> None:
    """status.txt — **창이 닫혀 있을 때의 유일한 근거.**

    `--auto status`가 이걸 읽어 "마지막으로 무슨 일이 있었나"를 답한다.
    사람이 읽는 평문이고 기계 판독은 보고 JSON이 담당한다(두 역할을 한 파일에
    섞으면 둘 다 어중간해진다).
    """
    lines = [
        f"dsync {__version__} 자동 동기화 상태",
        f"갱신     : {now:%Y-%m-%d %H:%M:%S}",
        f"프로그램 : {_REPO}",
    ]
    start = _parse(st.day_start)
    eod = _eod_time(st, auto)
    if start is not None and eod is not None:
        done = st.last_eod_date == now.date().isoformat()
        lines.append(f"오늘 기동: {start:%H:%M}")
        lines.append(f"퇴근 스윕: {eod:%H:%M} " + ("(완료)" if done else "(예정)"))
    lines.append("")
    lines.append("프로파일:")
    for name in names:
        rep = _read_report(name)
        try:
            seen = _last_sync_at(name)
        except Exception:      # noqa: BLE001 — 상태 표시가 루프를 죽이지 않는다
            seen = None
        mark = str(rep.get("outcome") or "-")
        extra = ""
        if _skipped_by_config_error(st, name):
            extra = "  [설정 오류로 쉬는 중]"
        mult = st.backoff_mult(name)
        if mult > 1.0:
            extra += f"  [주기 x{mult:g}]"
        when = f"{seen:%m-%d %H:%M}" if seen else "-"
        lines.append(f"  {name:12} 마지막 {when}  결과 {mark}{extra}")
    block = notify.format_block()
    if block:
        lines.append("")
        lines.append(block)
    try:
        dest = auto_dir() / "status.txt"
        os.makedirs(ext_path(dest.parent), exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
        with open(ext_path(tmp), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(ext_path(tmp), ext_path(dest))
    except OSError:
        pass


# 마커가 연속 몇 틱 안 보여야 '지웠다'로 볼 것인가. 2분 틱 기준 3회 = 6분.
# GDrive 재동기화·백신 격리·복원 진행 중에는 파일이 잠깐 사라진다 — 그 순간을
# 해제 의도로 읽으면 프로파일 정책이 사고로 꺼진다.
MARKER_ABSENT_TICKS = 3
# 한 틱에 자동 해제할 수 있는 최대 개수(I-A4). 넘으면 공통 마운트 장애로 본다.
MAX_AUTO_DISABLE = 1


def _reconcile(st: AutoState, names: list[str]) -> None:
    """마커 정합 — 무인 규칙으로 부른다(히스테리시스 + 재등록 금지 + 해제 상한).

    규칙 구현은 tools/sync_here.py 한 곳뿐이다. 러너가 자기 판정을 새로 만들면
    사람 경로와 무인 경로의 해석이 갈라진다.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_sync_here_for_runner", _REPO / "tools" / "sync_here.py")
    if spec is None or spec.loader is None:
        return
    sh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sh)

    profiles = {n: i for n, i in sh._load_profiles().items() if n in names}
    if not profiles:
        return

    override: dict[str, bool | None] = {}
    for name, info in profiles.items():
        observed = sh._marker_state(info["root"])
        if observed is None:
            override[name] = None
            st.bump_marker_absent(name, absent=False)
            continue
        streak = st.bump_marker_absent(name, absent=not observed)
        if observed:
            override[name] = True
        else:
            # 아직 확신이 안 서면 '판단 불가'로 넘긴다 — config를 안 바꾸는
            # 기존 경로에 그대로 사상되므로 새 상태를 만들지 않는다.
            override[name] = False if streak >= MARKER_ABSENT_TICKS else None
            if override[name] is None:
                print(f"    (마커 없음 {streak}/{MARKER_ABSENT_TICKS}회 — "
                      f"{name}: 아직 해제하지 않습니다)")

    sh.reconcile_markers(profiles, state_override=override,
                         allow_reenable=False, max_disable=MAX_AUTO_DISABLE)


_idle_open = False


def _print_idle(text: str) -> None:
    """대기 줄을 제자리에서 갱신. 이전 줄이 더 길었을 때 잔상이 남지 않게 채운다."""
    global _idle_open
    sys.stdout.write("\r" + text.ljust(78))
    sys.stdout.flush()
    _idle_open = True


def _end_idle() -> None:
    """제자리 갱신 중이던 줄을 확정하고 다음 출력을 새 줄에서 시작한다."""
    global _idle_open
    if _idle_open:
        sys.stdout.write("\n")
        sys.stdout.flush()
        _idle_open = False


def _eod_time(st: AutoState, auto: dict) -> _dt.datetime | None:
    """오늘의 퇴근 스윕 시각. 기동 시각 + 근무시간 - 15분."""
    start = _parse(st.day_start)
    if start is None:
        return None
    return start + _dt.timedelta(hours=float(auto["work_hours"])) - EOD_LEAD


def _next_event(st: AutoState, auto: dict, names: list[str],
                now: _dt.datetime) -> str:
    """대기 줄에 붙일 '다음에 무슨 일이 언제'. 기다리는 사람에게 필요한 정보다."""
    eod = _eod_time(st, auto)
    if (eod is not None and now < eod and now.weekday() < 5
            and st.last_eod_date != now.date().isoformat()):
        return f"  다음 동기화 {_next_due(st, auto, names, now)} · 퇴근 스윕 {eod:%H:%M}"
    return f"  다음 동기화 {_next_due(st, auto, names, now)}"


def _next_due(st: AutoState, auto: dict, names: list[str],
              now: _dt.datetime) -> str:
    """가장 먼저 차례가 오는 시각(HH:MM). 못 구하면 '-'."""
    interval = float(auto["min_interval_sec"])
    soonest: _dt.datetime | None = None
    for name in names:
        try:
            seen = _last_sync_at(name)
        except Exception:      # noqa: BLE001
            continue
        if seen is None:
            return "곧"
        when = seen + _dt.timedelta(seconds=interval * st.backoff_mult(name))
        if soonest is None or when < soonest:
            soonest = when
    return f"{soonest:%H:%M}" if soonest else "-"


def _tick(st: AutoState, extra: list[str], only: list[str] | None) -> None:
    """틱 1회. 예외를 밖으로 내보내지 않는다 — 루프가 죽으면 안 된다."""
    auto = load_auto()
    names = auto_profiles(only)
    try:
        _reconcile(st, names)
    except Exception as exc:      # noqa: BLE001 — 정합 실패가 동기화를 막지 않는다
        print(f"    (마커 정합 건너뜀 — {type(exc).__name__}: {exc})")
    # 정합이 sync_mode를 껐을 수 있다 — 대상 목록을 다시 읽는다.
    now = _now()
    last_sync = _last_sync_map(names)
    # 조회에 실패한 프로파일은 이번 틱 대상에서 뺀다(fail-closed).
    names = [n for n in names if n in last_sync]
    d = decide(st, auto, names, now=now, last_sync=last_sync)

    stamp = f"{now:%H:%M:%S}"
    if d.kind == "idle":
        # 대기 줄은 **제자리에서 갱신**한다(줄바꿈 없이 \r). 2분마다 새 줄을
        # 쌓으면 8시간에 240줄이 흐르고 그 속에서 진짜 사건을 놓친다.
        # 시각이 계속 변하는 것 자체가 '멈춘 창'과 '조용한 창'을 구분하는
        # 신호이므로(QuickEdit 정지가 정확히 이렇게 드러난다) 지우지는 않는다.
        nxt = _next_event(st, auto, names, now)
        _print_idle(f"[{stamp}] 대기 중{nxt}")
    else:
        _end_idle()
        print(f"[{stamp}] {d.why}")

    if d.kind == "sweep_start":
        st.day_start = now.isoformat(timespec="seconds")
    if d.kind == "sweep_eod":
        st.last_eod_date = now.date().isoformat()

    for name in d.names:
        if _skipped_by_config_error(st, name):
            print(f"  - {name}: 설정 문제로 쉬는 중(config가 바뀌면 자동 재개)")
            continue
        print(f"  - {name} 동기화 중...")
        rc = _run_child(name, auto, extra)
        report = _read_report(name)
        kind, msg = _classify(rc, report)
        print(f"    {name}: {msg}")
        _apply_backoff(st, name, kind, report)
        _note_outcome(st, name, kind, msg, report, now)

    st.last_tick = now.isoformat(timespec="seconds")
    st.save()
    _write_status(st, auto, names, now)


def run_once(*, only: list[str] | None = None, extra: list[str] | None = None) -> int:
    """`--auto now` — 판정을 무시하고 대상 전부를 한 바퀴 돈다(진단용)."""
    from .launcher import disable_quickedit
    disable_quickedit()
    # run_loop과 같은 이유 + 하나 더: 자식이 부모의 stdout 핸들을 물려받으므로,
    # 부모 출력이 버퍼에 남아 있으면 자식 출력이 **먼저** 찍혀 순서가 뒤집힌다
    # (파이프로 캡처할 때 실제로 그렇게 나온다).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass
    auto = load_auto()
    names = auto_profiles(only)
    if not names:
        print("자동 대상 프로파일이 없습니다 — synchere.bat --auto on 으로 켜세요.")
        return 2
    st = AutoState()
    print(f"== 자동 동기화 1회 (dsync {__version__})")
    print(f"   프로그램 폴더: {_REPO}")
    now = _now()
    for name in names:
        print(f"  - {name} 동기화 중...")
        rc = _run_child(name, auto, extra or [])
        report = _read_report(name)
        _kind, msg = _classify(rc, report)
        print(f"    {name}: {msg}")
        _apply_backoff(st, name, _kind, report)
        _note_outcome(st, name, _kind, msg, report, now)
    # last_tick은 쓰지 않는다 — 이건 틱이 아니라 진단용 1회 실행이다. 여기서
    # 시각을 남기면 루프가 '방금 틱이 있었다'고 보고 기동 스윕을 건너뛴다.
    # 다음 실행 시각 판단은 상태 DB의 last_sync_at이 하므로 잃는 정보도 없다.
    st.save()
    _write_status(st, auto, names, now)
    return 0


def run_loop(*, only: list[str] | None = None, extra: list[str] | None = None) -> int:
    """`--auto loop` — 창 안에서 도는 본체. Ctrl+C 또는 창 닫기로 끝난다."""
    from .launcher import disable_quickedit

    quickedit = disable_quickedit()
    # 매 틱의 시각 출력이 '멈춘 창'을 알아보는 유일한 신호다. 파이프·파일로
    # 리다이렉트하면 기본이 블록 버퍼링이라 몇 KB가 찰 때까지 아무것도 안 보인다 —
    # 진단하려고 리다이렉트한 사람이 정확히 그 신호를 잃는다.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass
    auto = load_auto()
    tick_sec = max(30, int(auto["tick_sec"]))

    print("=" * 66)
    print(f" Dooray Sync 자동 동기화 (dsync {__version__})")
    print("=" * 66)
    # 어느 폴더로 도는지 반드시 보이게 한다 — 이 PC는 탐색 사슬이 git 작업
    # 트리를 고르므로, 개발 중인 코드가 하루 종일 실계정에 도는 사고가 실재한다.
    print(f" 프로그램 폴더 : {_REPO}")
    print(f" 설정          : {config_path()}")
    print(f" 상태          : {auto_dir()}")
    # 퇴근 스윕은 기동 시각에 따라 매일 달라진다 — 공식이 아니라 **오늘 몇 시인지**를
    # 보여 준다. 사람이 알고 싶은 것은 규칙이 아니라 시각이다.
    st_preview = AutoState()
    eod_today = _eod_time(st_preview, auto)
    if eod_today is None:
        # 아직 기동 판정 전(첫 틱에서 정해진다) — 지금 켠 것으로 가정해 미리 보인다.
        eod_guess = (_now() + _dt.timedelta(hours=float(auto["work_hours"]))
                     - EOD_LEAD)
        eod_text = f"{eod_guess:%H:%M} 예정(첫 틱에 확정)"
    else:
        eod_text = f"{eod_today:%H:%M}"
        if _now().weekday() >= 5:
            eod_text += " (주말이라 건너뜀)"
    print(f" 근무시간      : {auto['work_hours']:g}시간 → 퇴근 스윕 {eod_text}")
    print(f" 평시 주기     : {int(auto['min_interval_sec']) // 60}분 · 틱 {tick_sec}초")
    if not quickedit:
        print(" [주의] QuickEdit을 끄지 못했습니다 — 창 안을 클릭하면 동기화가")
        print("        멈출 수 있습니다. 멈추면 아무 키나 누르세요.")
    print(" 이 창을 닫으면 자동 동기화가 멈춥니다. 급한 파일은 폴더의")
    print(" synchere.bat 을 더블클릭하면 즉시 동기화됩니다.")
    print("=" * 66)
    print()

    st = AutoState()
    stop = False

    def _on_signal(_sig, _frm) -> None:
        nonlocal stop
        stop = True

    try:
        signal.signal(signal.SIGINT, _on_signal)
    except (ValueError, OSError):
        pass

    # KeyboardInterrupt는 루프 전체를 감싼다. 대부분의 시간을 대기로 보내므로
    # Ctrl+C는 십중팔구 sleep 중에 들어온다 — 거기서 안 잡으면 창 닫기가
    # 트레이스백으로 끝나고, 비개발자에게는 그것이 곧 '고장'으로 읽힌다.
    # (signal 핸들러가 있으면 그쪽이 먼저 받지만, 등록 실패한 환경도 있다.)
    try:
        while not stop:
            try:
                _tick(st, extra or [], only)
            except Exception as exc:   # noqa: BLE001 — 루프는 어떤 이유로도 죽지 않는다
                print(f"[{_now():%H:%M:%S}] 틱 실패 — {type(exc).__name__}: {exc}")
                print("    (다음 틱에 다시 시도합니다)")
            for _ in range(tick_sec):
                if stop:
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass

    _end_idle()
    print()
    print("자동 동기화를 멈췄습니다. 다시 켜려면 이 창을 닫고 다음 로그온을")
    print("기다리거나, synchere.bat --auto loop 를 실행하세요.")
    return 0
