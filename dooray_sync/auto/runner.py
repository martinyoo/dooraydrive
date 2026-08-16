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
from pathlib import Path

from .. import __version__
from ..config import config_path, db_path, load_auto, load_config
from ..store.db import Store
from ..util.paths import ext_path
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
    """auto_sync=true인 프로파일 이름(이름순). only가 있으면 그 교집합.

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
    ]
    if only is not None:
        allow = {n.casefold() for n in only}
        names = [n for n in names if n.casefold() in allow]
    return sorted(names, key=str.casefold)


def _last_sync_at(name: str) -> _dt.datetime | None:
    """상태 DB의 마지막 sync 시각. **프로세스를 띄우지 않고** 읽기 전용으로 연다.

    일반 connect는 DB가 없을 때 빈 파일을 만든다 — 그러면 편입 게이트의
    '상태 DB 없음' 판정이 다음부터 통과해 버린다. mode=ro로 막는다.
    """
    import sqlite3
    path = db_path(name)
    if not os.path.exists(ext_path(path)):
        return None
    uri = "file:" + str(ext_path(path)).replace("\\", "/").replace("?", "%3f") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'last_sync_at'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return _parse(row[0]) if row and row[0] else None


def decide(st: AutoState, auto: dict, names: list[str], *, now: _dt.datetime,
           last_sync: dict[str, _dt.datetime | None]) -> Decision:
    """순수 판정 — 시계·상태·설정만 보고 무엇을 돌릴지 정한다(부작용 없음)."""
    if not names:
        return Decision("idle", [], "자동 대상 프로파일이 없습니다")

    prev = _parse(st.last_tick)
    gap = _dt.timedelta(hours=float(auto["day_gap_hours"]))
    if prev is None or now - prev >= gap:
        return Decision("sweep_start", list(names),
                        "기동(직전 틱과 간격이 벌어짐) — 전체 한 바퀴")

    start = _parse(st.day_start)
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


def _classify(rc: int) -> tuple[str, str]:
    """종료코드 → (분류, 사람이 읽는 말). exit 1은 고장이 아니다(I-A11) —
    일부 파일 실패는 정상 운용에서 늘 생기고, 다음 실행이 재시도한다."""
    return {
        0: ("ok", "완료"),
        1: ("partial", "일부 실패(다음 주기에 재시도)"),
        2: ("config", "설정 문제 — 자동 대상에서 건너뜁니다"),
        3: ("locked", "다른 실행이 사용 중 — 다음 틱에"),
        4: ("held", "보류(사람 확인 필요)"),
    }.get(rc, ("error", f"종료코드 {rc}"))


def _apply_backoff(st: AutoState, name: str, kind: str) -> None:
    """rate limit 관측이 있으면 주기를 늘리고, 깨끗하면 서서히 되돌린다."""
    report = auto_dir() / "last" / f"{name}.json"
    limited = 0
    try:
        import json
        with open(ext_path(report), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
        rate = data.get("rate") or {}
        limited = int(rate.get("rate_limited") or 0)
    except (OSError, ValueError, TypeError):
        pass
    cur = st.backoff_mult(name)
    if limited > 0:
        st.set_backoff_mult(name, cur * 2)
        print(f"    (rate limit {limited}회 관측 — 이 폴더 주기를 "
              f"{st.backoff_mult(name):g}배로 늘립니다)")
    elif kind in ("ok", "partial") and cur > 1.0:
        st.set_backoff_mult(name, max(1.0, cur * 0.75))


def _tick(st: AutoState, extra: list[str], only: list[str] | None) -> None:
    """틱 1회. 예외를 밖으로 내보내지 않는다 — 루프가 죽으면 안 된다."""
    auto = load_auto()
    names = auto_profiles(only)
    now = _now()
    last_sync = {n: _last_sync_at(n) for n in names}
    d = decide(st, auto, names, now=now, last_sync=last_sync)

    stamp = f"{now:%H:%M:%S}"
    if d.kind == "idle":
        # 시각이 계속 갱신되는 것이 '멈춘 창'과 '조용한 창'을 구분한다
        # (QuickEdit 정지가 정확히 이렇게 드러난다).
        nxt = "" if not names else "  다음 차례를 기다리는 중"
        print(f"[{stamp}] 대기{nxt}")
    else:
        print(f"[{stamp}] {d.why}")

    if d.kind == "sweep_start":
        st.day_start = now.isoformat(timespec="seconds")
    if d.kind == "sweep_eod":
        st.last_eod_date = now.date().isoformat()

    for name in d.names:
        print(f"  - {name} 동기화 중...")
        rc = _run_child(name, auto, extra)
        kind, msg = _classify(rc)
        print(f"    {name}: {msg}")
        _apply_backoff(st, name, kind)

    st.last_tick = now.isoformat(timespec="seconds")
    st.save()


def run_once(*, only: list[str] | None = None, extra: list[str] | None = None) -> int:
    """`--auto now` — 판정을 무시하고 대상 전부를 한 바퀴 돈다(진단용)."""
    from .launcher import disable_quickedit
    disable_quickedit()
    auto = load_auto()
    names = auto_profiles(only)
    if not names:
        print("자동 대상 프로파일이 없습니다 — synchere.bat --auto on 으로 켜세요.")
        return 2
    st = AutoState()
    print(f"== 자동 동기화 1회 (dsync {__version__})")
    print(f"   프로그램 폴더: {_REPO}")
    for name in names:
        print(f"  - {name} 동기화 중...")
        rc = _run_child(name, auto, extra or [])
        _kind, msg = _classify(rc)
        print(f"    {name}: {msg}")
        _apply_backoff(st, name, _kind)
    st.last_tick = _now().isoformat(timespec="seconds")
    st.save()
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
    print(f" 근무시간      : {auto['work_hours']:g}시간 "
          f"(퇴근 스윕 = 기동 + {auto['work_hours']:g}h - 15분, 평일만)")
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

    print()
    print("자동 동기화를 멈췄습니다. 다시 켜려면 이 창을 닫고 다음 로그온을")
    print("기다리거나, synchere.bat --auto loop 를 실행하세요.")
    return 0
