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
import hashlib
import os
import signal
import subprocess
import sys
import time
import unicodedata
import uuid
from pathlib import Path

from .. import __version__
from ..config import config_path, db_path, load_auto
from ..util.paths import ext_path
from . import keys, notify
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


def _due_at(st: AutoState, name: str, seen: _dt.datetime | None, interval: float,
            now: _dt.datetime) -> _dt.datetime | None:
    """그 프로파일이 다음에 평시 차례가 되는 시각. None 이면 지금 바로.

    기준은 마지막 동기화 시각(상태 DB 의 last_sync_at)이다. **대량 삭제 보류는 예외다** —
    그 실행은 계획 단계에서 멈춰 last_sync_at 을 남기지 않으므로, 마지막 동기화만 보면
    보류된 폴더는 매 틱 차례가 된다. 2026-11-11 실측: WRITING 이 2분마다 같은 보류를
    776회 반복했다. '주기 x8'은 숫자뿐이었고(기준점이 닷새 전이라 언제나 지나 있었다),
    화면이 그 출력으로 계속 흘러 'y' 를 누를 대기 줄이 한 번도 나오지 않았다 — 사용자는
    0.3.3 으로 올린 뒤에도 "같은 현상"을 보았다. 보류의 결과는 사람이 손대기 전까지
    같으므로, 보류된 폴더는 **마지막 시도(보류 시각)부터** 주기를 센다.

    보류 시각이 지금보다 뒤면(시계가 뒤로 간 PC) 그 기준은 버린다 — 미래 시각에 묶이면
    시계가 따라잡을 때까지 그 폴더가 영영 안 돈다. 다음 실행이 지금 시계로 다시 찍는다.
    """
    marks: list[_dt.datetime] = []
    if seen is not None:
        marks.append(seen)
    hold = _bulk_hold(st, name)
    if hold:
        at = _parse(str(hold.get("at") or ""))
        if at is not None and at <= now:
            marks.append(at)
    if not marks:
        return None
    return max(marks) + _dt.timedelta(seconds=interval * st.backoff_mult(name))


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
        when = _due_at(st, name, seen, interval, now)
        if when is None:
            due.append((0.0, name))
        elif now >= when:
            due.append((seen.timestamp() if seen is not None else 0.0, name))
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


def _apply_backoff(st: AutoState, name: str, kind: str, report: dict, *,
                   quiet: bool = False) -> None:
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
        if not quiet:      # 대량 삭제 보류는 알림 블록이 '다시 확인' 시각을 따로 적는다
            print(f"    (연속 {streak}회 같은 결과 — 주기를 "
                  f"{st.backoff_mult(name):g}배로 늘립니다)")
    elif kind == "ok" and cur > 1.0:
        st.set_backoff_mult(name, max(1.0, cur * 0.75))


def _config_fingerprint() -> str:
    """config 파일의 내용 지문 — "바뀌었는가"를 판정하는 유일한 근거.

    예전에는 mtime 이었다. 갈아탄 이유는 실측이다(2026-09-14). **내용이 매번
    달라지는 저장 300회 중 mtime 은 변경을 173회(58%) 놓쳤다**(내용 지문은 0회).
    Windows 시스템 시계가 약 15.6ms 단위로 움직이는데 config 저장은 그보다
    훨씬 빨리 끝나기 때문이다. 그래서 "config 가 바뀌면 쉬는 상태가 자동으로
    풀린다"는 약속이 사실상 동전 던지기였고, 같은 이유로 회귀 테스트가 전체
    실행에서 간헐적으로 빨간불이 됐다 — 릴리스 판정('테스트 전건 통과')이
    흔들린다는 뜻이다.

    config 는 작은 파일이라 틱마다 해시해도 비용이 문제되지 않는다. 시간이
    아니라 내용을 보는 것이 애초에 물어야 했던 질문이기도 하다.
    """
    try:
        with open(ext_path(config_path()), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def _note_outcome(st: AutoState, name: str, kind: str, msg: str,
                  report: dict, now: _dt.datetime) -> None:
    """실행 결과를 통지로 옮긴다. 원인이 해소되면 스스로 지워진다."""
    ts = now.isoformat(timespec="seconds")
    entry = st.profile(name)

    if kind == "config":
        # 같은 거부를 2분마다 영원히 반복하지 않는다. config가 바뀌거나 사람이
        # 그 폴더에서 실행하기 전까지 이 프로파일의 자동 실행을 쉰다.
        entry["config_error_fp"] = _config_fingerprint()
        notify.add(name, "config", "설정 문제로 자동 실행을 멈췄습니다 - "
                                   "그 폴더에서 synchere.bat을 한 번 실행하세요", ts=ts)
        return
    entry.pop("config_error_fp", None)
    entry.pop("config_error_mtime", None)   # 옛 키 청소(0.2.0 이전 state.json)

    from ..api.faults import ADVICE, LABEL, Fault, is_transient

    if kind == Fault.HELD:
        why = str(report.get("held_reason") or "")
        if why == "bulk_delete":
            # 이 보류는 그 폴더의 동기화를 **통째로** 멈춘다 — 그 사실과, 어디서 풀 수 있는지
            # (명령이 아니라 누를 곳)를 함께 적는다.
            info = report.get("bulk_delete") if isinstance(report.get("bulk_delete"), dict) else {}
            what = str(info.get("why") or "") or "삭제 대상이 임계를 넘음"
            notify.add(name, "held",
                       f"대량 삭제 보류({what}) - 이 폴더는 동기화가 멈춰 있습니다. "
                       f"자동 동기화 창에서 y 를 누르거나 그 폴더의 synchere.bat 을 "
                       f"실행하면 계획을 보고 처리할 수 있습니다", ts=ts)
            return
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
    stamp = st.profile(name).get("config_error_fp")
    if not stamp:
        # 옛 키(config_error_mtime)만 있는 state.json 은 여기서 자연히 풀린다 —
        # 한 틱 더 돌아 보고 여전히 설정 문제면 새 지문으로 다시 쉰다(fail-open).
        return False
    fp = _config_fingerprint()
    if not fp:
        return False          # 지문을 못 구하면 쉬게 두지 않는다
    return str(stamp) == fp


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
        if _bulk_hold(st, name):
            extra += "  [대량 삭제 보류 — 동기화 멈춤]"
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
    sh = _sync_here()
    if sh is None:
        return

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


def _display_width(text: str) -> int:
    """콘솔에서 차지하는 칸 수. 한글은 2칸이다 — 글자 수로 채우면 한글 줄이 80칸을
    넘겨 다음 줄로 접히고, 그러면 제자리 갱신(\r)이 매번 새 줄을 쌓는다. 폭이 모호한
    글자('·' 등)도 한국어 콘솔(CP949)에서는 2칸으로 그리므로 2로 센다."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 1 for c in text)


def _print_idle(text: str) -> None:
    """대기 줄을 제자리에서 갱신. 이전 줄이 더 길었을 때 잔상이 남지 않게 채운다."""
    global _idle_open
    sys.stdout.write("\r" + text + " " * max(0, 78 - _display_width(text)))
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
        when = _due_at(st, name, seen, interval, now)
        if when is None:
            return "곧"
        if soonest is None or when < soonest:
            soonest = when
    return f"{soonest:%H:%M}" if soonest else "-"


# --------------------------------------------------------------------------
# 대량 삭제 보류 — 이 창에서 y 로 처리하기 (2026-11-10 사용자 요구)
# --------------------------------------------------------------------------
# 대량 삭제 게이트(core.planner)는 그 프로파일의 동기화를 **통째로** 멈춘다. 예전에는 이
# 창이 "--allow-bulk-delete 를 붙여 다시 실행하세요"로 끝났고, 사람이 그 폴더로 가서 다른
# 창을 열어야 했다 — 그동안 같은 보류가 240틱 넘게 반복됐다(WRITING 실측). 이제 이 창에서
# 처리한다. 지키는 것 넷:
#   1. 루프를 붙잡지 않는다. 묻는 자리는 사람이 y 를 누를 때만 열린다 — 보류를 만나자마자
#      입력을 기다리면 다른 폴더 전부의 자동 동기화가 사람 대기의 인질이 된다(sync_here 의
#      '설계 사유 1'과 같은 이유). 키는 틱 사이 대기 중에만, 보류가 있을 때만 본다.
#   2. 먼저 보여 주고 묻는다. 미리보기(dry-run)로 무엇이 몇 건 사라지는지 보인 뒤 y/N.
#      기본값 '아니오', OFFER_ANSWER_TIMEOUT 안에 답이 없으면 '아니오', Ctrl+C 도 '아니오'.
#   3. 본 계획만 실행한다. 실행은 미리보기의 삭제 지문(--expect-delete-fp)을 들고 가서,
#      그 사이 계획이 바뀌었으면 CLI 가 아무것도 하지 않는다.
#   4. 무인 강등은 그대로다. 미리보기·실행 둘 다 --unattended(충돌 보류·로컬 붕괴 보류)를
#      유지하고, 사람이 본 대량 삭제만 허용한다(--allow-bulk-delete, 무인 등급화 없음).
# 흐름은 사람 창(synchere.bat)과 같은 함수 하나다 — tools/sync_here.py 의 bulk_delete_flow.

OFFER_ANSWER_TIMEOUT = 300     # 계획을 본 뒤 '지울까요?'에 답할 시간(초)
OFFER_OPEN_TIMEOUT = 60        # --auto now 의 '지금 처리할까요?'(대기 중 키를 볼 수 없는 경로)
_OFFER_ACTIVE = False          # 묻는 중 — Ctrl+C 를 '아니오'로 받는다
_OFFER_CANCEL = False
_SH = None


def _sync_here():
    """tools/sync_here.py — 사람 경로의 규칙(마커 정합·대량 삭제 흐름)의 단일 정본.
    러너가 자기 판정을 새로 만들면 사람 경로와 무인 경로의 해석이 갈라진다."""
    global _SH
    if _SH is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_sync_here_for_runner", _REPO / "tools" / "sync_here.py")
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SH = mod
    return _SH


def _sigint_should_stop() -> bool:
    """Ctrl+C 를 어떻게 받을지. 묻는 중이면 그 질문의 '아니오'(False), 아니면 루프 정지."""
    global _OFFER_CANCEL
    if _OFFER_ACTIVE:
        _OFFER_CANCEL = True
        return False
    return True


def _bulk_hold(st: AutoState, name: str) -> dict | None:
    hold = st.profile(name).get("bulk_hold")
    return hold if isinstance(hold, dict) else None


def _bulk_holds(st: AutoState, names: list[str]) -> list[str]:
    """지금 자동 대상 중 대량 삭제로 멈춰 있는 프로파일(이름순 — names 의 순서)."""
    return [n for n in names if _bulk_hold(st, n)]


_GATE_PASSED = frozenset({"ok", "no_changes", "partial"})


def _update_bulk_hold(st: AutoState, name: str, report: dict, now: _dt.datetime,
                      extra: list[str]) -> dict | None:
    """실행 보고로 그 프로파일의 대량 삭제 보류를 세우거나 지운다. 세웠으면 그 내용.

    지우는 것은 **게이트를 지나 실행까지 간** 결과일 때뿐이다(ok·no_changes·partial).
    네트워크 끊김·잠금 경합 같은 실패는 게이트까지 가지 못한 것이라 보류가 풀렸는지
    모른다 — 모르는 것을 '풀렸다'로 읽으면 사람에게 물을 길이 사라진다.
    dry-run 루프는 어차피 아무것도 지우지 않으므로 보류를 세우지 않는다.
    """
    entry = st.profile(name)
    outcome = str(report.get("outcome") or "")
    if outcome == "aborted_bulk_delete" and "--dry-run" not in extra:
        info = report.get("bulk_delete") if isinstance(report.get("bulk_delete"), dict) else {}
        why = str(info.get("why") or "")
        if not why:
            first = str(report.get("error") or "").strip().splitlines()
            why = first[0] if first else "삭제 대상이 임계를 넘음"
        try:
            count = int(info.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        hold = {"why": why, "count": count, "at": now.isoformat(timespec="seconds")}
        entry["bulk_hold"] = hold
        return hold
    if outcome in _GATE_PASSED and _bulk_hold(st, name):
        # 사람이 원인을 바로잡아(휴지통에서 되살리기 등) 다음 실행이 게이트를 지났다 —
        # 다른 해소 경로와 똑같이 주기를 바로 되돌린다(서서히 줄이면 반나절 드문드문).
        _release_bulk_hold(st, name)
    return None


def _release_bulk_hold(st: AutoState, name: str) -> None:
    """보류가 풀렸다 — 보류 표시·알림을 지우고, 보류로 늘어난 주기를 **바로** 되돌린다.

    보류는 같은 결과를 반복하므로 거의 언제나 주기 x8(최대)까지 늘어 있다. 그대로 두면
    서서히(x0.75씩) 줄어 반나절 넘게 드문드문 동기화한다 — 화면은 '평소대로'라고 하는데.
    """
    entry = st.profile(name)
    entry.pop("bulk_hold", None)
    entry["fail_streak"] = 0
    st.set_backoff_mult(name, 1.0)
    notify.clear(name, "held")


def _settle_bulk_holds(st: AutoState,
                       last_sync: dict[str, _dt.datetime | None]) -> None:
    """다른 곳에서 풀린 보류를 지운다 — 그 폴더의 synchere.bat 에서 동의해 지웠거나,
    사람이 원인을 바로잡아 다음 실행이 게이트를 지났을 때.

    근거는 상태 DB 의 last_sync_at 이다. 대량 삭제로 멈춘 실행은 그 값을 남기지 않고
    (계획 단계에서 멈춘다), 게이트를 지난 실행만 남긴다. 그러니 보류를 세운 뒤에 그 값이
    새로 찍혔으면 보류는 풀린 것이다. 이 루프가 state.json 의 유일한 기록자로 남는다 —
    synchere.bat 이 state.json 을 같이 쓰면 둘이 서로의 갱신을 덮어쓴다.
    """
    for name, seen in last_sync.items():
        hold = _bulk_hold(st, name)
        if not hold or seen is None:
            continue
        at = _parse(str(hold.get("at") or ""))
        if at is not None and seen > at:
            _release_bulk_hold(st, name)


def _print_bulk_banner(holds: list[tuple[str, dict | None]], *, key_hint: bool = True,
                       recheck: dict[str, _dt.datetime | None] | None = None,
                       now: _dt.datetime | None = None) -> None:
    """보류를 알리는 블록. 알린 뒤에는 그 전에 쌓인 키를 버린다 — 보류를 보기도 전에
    눌린 y 가 '계획 보기'를 열면 안 된다.

    끝은 **질문**이다([y/N]). "y 를 누르면 ~합니다"라는 안내문은 사용자 눈에 질문으로
    읽히지 않았다(2026-11-11 — "Y/N 으로 묻게 해 달라" 요구 뒤에도 "같은 현상"). 답은
    대기 중 아무 때나 받는다 — 묻는 자리가 루프를 붙잡지 않는다는 조건은 그대로다."""
    print("    " + "-" * 62)
    for name, hold in holds:
        why = (hold or {}).get("why") or "삭제 대상이 임계를 넘음"
        print(f"    {name}: 대량 삭제로 판단해 이 폴더의 동기화를 멈췄습니다 — {why}.")
        print(f"      아무것도 지우지 않았습니다.{_recheck_text(recheck, name, now)}")
    if key_hint:
        print("    지울 계획을 보고 처리할까요? [y/N]")
        print("      y — 지울 계획을 먼저 보여 주고, 정말 지울지 한 번 더 묻습니다.")
        print("      답하지 않으면 보류된 채로 두고, 다른 폴더의 자동 동기화는 계속합니다.")
    print("    " + "-" * 62)
    keys.drain()


def _recheck_text(recheck: dict[str, _dt.datetime | None] | None, name: str,
                  now: _dt.datetime | None) -> str:
    """'그대로 두면 HH:MM 에 다시 확인합니다' — 이미 지난 시각은 적지 않는다.

    창을 다음 날 아침에 다시 띄우면 어제 세운 보류의 시각(어제 21:30)이 오늘 밤처럼
    읽혔다(2026-11-11 검토 재현). 지났으면 '곧', 오늘이 아니면 날짜를 붙인다."""
    if recheck is None or name not in recheck:
        return ""
    when = recheck[name]
    if when is None or (now is not None and when <= now):
        return " 곧 다시 확인합니다."
    fmt = "%H:%M" if now is None or when.date() == now.date() else "%m-%d %H:%M"
    return f" 그대로 두면 {when:{fmt}} 에 다시 확인합니다."


_NO_KEYS = frozenset({"n", "N", "ㅜ", "\x1b"})
_NO_WORDS = frozenset({"", "n", "no", "아니", "아니오", "아니요"})


def _first_answer(typed: str, line: str = "") -> tuple[str, str]:
    """대기 중에 눌린 키들에서 [y/N] 답을 고른다 — (답, 아직 Enter 를 기다리는 글자).
    답은 'yes'|'no'|'' 이고 먼저 나온 답이 이긴다.

    - y·Y·ㅛ 는 누르는 즉시 '예'다(Enter 불필요 — 여는 것은 아무것도 바꾸지 않는
      미리보기다). 한글 상태의 ㅛ 는 조합 중이라 Enter 로 확정될 때 함께 들어온다.
    - n·N·ㅜ·Esc 는 즉시 '아니오'.
    - 그 밖의 글자는 모았다가 Enter 에서 한 줄로 읽는다. 「예」「네」「yes」는 '예'
      (지울지 묻는 질문 keys.ask 와 같은 낱말), 빈 줄·「아니오」는 '아니오', 나머지는
      무시한다. 2026-11-11 검토: '네' + Enter 가 이 질문에선 '아니오', 바로 다음 질문에선
      '예'로 읽혔다 — 한 흐름의 두 [y/N] 이 같은 답을 반대로 읽으면 안 된다.
    지울지는 여기서 정하지 않는다 — 그것은 미리보기 뒤 keys.ask 가 따로 묻는다."""
    for c in typed:
        if c in keys.YES_KEYS:
            return "yes", ""
        if c in _NO_KEYS:
            return "no", ""
        if c in ("\r", "\n"):
            word, line = line.strip().casefold(), ""
            if word in keys.YES_WORDS:
                return "yes", ""
            if word in _NO_WORDS:
                return "no", ""
            continue
        if c.isprintable():
            line = (line + c)[-16:]
    return "", line


def _flow_runner(name: str):
    """bulk_delete_flow 가 부를 자식 실행기 — 루프의 자식과 같은 규율(중립 작업 폴더,
    닫힌 stdin, 같은 환경). 콘솔 출력은 그대로 이 창에 나온다(사람이 계획을 본다)."""
    def run(args: list[str]) -> int:
        cmd = [sys.executable, "-P", "-m", "dooray_sync.cli.main",
               args[0], "-p", name, *args[1:]]
        try:
            return subprocess.call(cmd, cwd=_neutral_cwd(), env=_child_env(),
                                   stdin=subprocess.DEVNULL)
        except OSError as exc:
            print(f"    [오류] 자식 실행 실패 — {type(exc).__name__}: {exc}")
            return 1
    return run


def _handle_bulk_holds(st: AutoState, auto: dict, names: list[str],
                       extra: list[str]) -> None:
    """멈춰 있는 프로파일마다 계획을 보여 주고 묻는다. 루프를 죽이는 예외를 내지 않는다.

    이 동안에는 다른 폴더의 틱이 잠시 기다린다 — 사람이 직접 연 자리이고, 길어야
    미리보기 + 답할 시간(OFFER_ANSWER_TIMEOUT) + 실행이다. 답이 없거나(시간 초과)
    Ctrl+C 면 남은 보류는 묻지 않는다 — 자리에 없는 사람에게 하나씩 5분씩 쓰지 않게.
    """
    global _OFFER_ACTIVE, _OFFER_CANCEL
    holds = _bulk_holds(st, names)
    if not holds:
        return
    _end_idle()
    try:
        sh = _sync_here()
    except Exception as exc:   # noqa: BLE001 — 불러오기 실패가 루프를 죽이면 안 된다
        print(f"    (대량 삭제 처리 흐름을 불러오지 못했습니다: {type(exc).__name__}: {exc})")
        return
    if sh is None or not hasattr(sh, "bulk_delete_flow"):
        print("    (대량 삭제 처리 흐름을 불러오지 못했습니다 — 그 폴더의 synchere.bat 으로도 "
              "같은 질문이 나옵니다)")
        return
    print()
    print("=" * 66)
    print(f" 대량 삭제 보류 {len(holds)}개 — 하나씩 계획을 보여 주고 묻습니다.")
    print(" 미리보기는 아무것도 바꾸지 않습니다. 답하는 동안 다른 폴더의 틱은 잠시 기다립니다.")
    print("=" * 66)
    base = ["--unattended", *[a for a in extra if a != "--dry-run"]]
    _OFFER_ACTIVE, _OFFER_CANCEL = True, False
    try:
        for k, name in enumerate(holds, 1):
            if _OFFER_CANCEL:
                break
            hold = _bulk_hold(st, name) or {}
            print()
            print(f"({k}/{len(holds)}) [{name}] {hold.get('why') or ''}")
            answered = {"r": ""}

            def _ask(question: str, _a=answered) -> bool:
                r = keys.ask(question, timeout=OFFER_ANSWER_TIMEOUT,
                             cancelled=lambda: _OFFER_CANCEL)
                _a["r"] = r
                return r == "yes"

            result, rc = sh.bulk_delete_flow(
                name, run=_flow_runner(name), ask=_ask, base=base,
                report_path=str(auto_dir() / "last" / f"{name}.json"))
            now = _now()
            if result == sh.BULK_DONE:
                report = _read_report(name)
                kind, msg = _classify(rc, report)
                _note_outcome(st, name, kind, msg, report, now)
                _release_bulk_hold(st, name)
            elif result == sh.BULK_NOTHING:
                _release_bulk_hold(st, name)
                print("  보류를 풉니다 — 다음 차례에 평소대로 동기화합니다.")
            else:
                print("  보류는 그대로 둡니다 — 이 창에서 y 를 다시 누르면 다시 묻습니다.")
            if answered["r"] in ("timeout", "cancel", "unavailable"):
                break
    except (Exception, KeyboardInterrupt) as exc:   # noqa: BLE001 — 루프는 죽지 않는다
        print()
        print(f"    (삭제 보류 처리를 마치지 못했습니다 — 보류는 그대로입니다: "
              f"{type(exc).__name__}: {exc})")
    finally:
        _OFFER_ACTIVE, _OFFER_CANCEL = False, False
        st.save()
        try:
            _write_status(st, auto, names, _now())
        except Exception:   # noqa: BLE001 — 상태 표시가 처리 결과를 뒤집지 않는다
            pass
    print()


def _idle_wait(st: AutoState, seconds: int, *, should_stop, only: list[str] | None,
               extra: list[str], clock=time.monotonic, sleep=time.sleep) -> None:
    """틱 사이 대기. 대량 삭제 보류가 있을 때만 키를 보고, y 면 그 자리에서 처리한다.

    보류가 없으면 키를 읽지 않는다(예전 동작 그대로 — 쌓인 키는 건드리지 않는다).
    y 를 처리하고 나면 남은 대기를 버리고 다음 틱으로 간다 — 방금 처리한 결과를
    대기 줄이 곧바로 보여 주게.
    """
    end = clock() + max(0, int(seconds))
    names = auto_profiles(only) if "--dry-run" not in extra else []
    said_no = False
    line = ""
    while not should_stop() and clock() < end:
        if names and _bulk_holds(st, names):
            answer, line = _first_answer(keys.drain(), line)
            if answer == "no" and not said_no:
                # [y/N] 로 물었으니 N 에도 대답한다 — 아무 반응이 없으면 눌린 줄 모른다.
                said_no = True
                _end_idle()
                print("    보류된 채로 둡니다 — 이 창에서 y 를 누르면 언제든 다시 묻습니다.")
            if answer == "yes":
                # 설정 파일이 잠깐 잠긴 순간(백신 검사·편집기 저장) y 를 눌러도 루프 전체가
                # 죽으면 안 된다 — 틱과 같은 규율로 감싼다(2026-11-10 검토에서 재현).
                try:
                    _handle_bulk_holds(st, load_auto(), names, extra)
                except Exception as exc:   # noqa: BLE001
                    _end_idle()
                    print(f"    (삭제 보류 처리를 시작하지 못했습니다 — 보류는 그대로입니다: "
                          f"{type(exc).__name__}: {exc})")
                return
            sleep(0.2)
        else:
            sleep(1)


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
    _settle_bulk_holds(st, last_sync)
    d = decide(st, auto, names, now=now, last_sync=last_sync)

    stamp = f"{now:%H:%M:%S}"
    if d.kind == "idle":
        # 대기 줄은 **제자리에서 갱신**한다(줄바꿈 없이 \r). 2분마다 새 줄을
        # 쌓으면 8시간에 240줄이 흐르고 그 속에서 진짜 사건을 놓친다.
        # 시각이 계속 변하는 것 자체가 '멈춘 창'과 '조용한 창'을 구분하는
        # 신호이므로(QuickEdit 정지가 정확히 이렇게 드러난다) 지우지는 않는다.
        nxt = _next_event(st, auto, names, now)
        held = _bulk_holds(st, names) if "--dry-run" not in extra else []
        if held:
            nxt += f" | 삭제 보류 {len(held)}개: y/N"
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
        # 보류 시각은 그 실행이 **끝난** 때다. 틱 시작 시각을 쓰면, 스윕에서 앞 프로파일이
        # 도는 사이 그 폴더를 손으로 동기화한 기록(last_sync_at)이 '보류 뒤에 풀렸다'로
        # 잘못 읽혀 방금 세운 보류가 다음 틱에 지워진다(검토 재현).
        ended = _now()
        hold = _update_bulk_hold(st, name, report, ended, extra)
        if hold:
            msg = f"보류 — 대량 삭제({hold['why']}). 이 폴더는 처리할 때까지 동기화되지 않습니다"
        print(f"    {name}: {msg}")
        # 주기를 먼저 고친다 — 알림 블록이 적는 '다시 확인' 시각이 고친 주기로 계산돼야 한다.
        _apply_backoff(st, name, kind, report, quiet=bool(hold))
        if hold:
            when = _due_at(st, name, last_sync.get(name),
                           float(auto["min_interval_sec"]), ended)
            _print_bulk_banner([(name, hold)], recheck={name: when}, now=ended)
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
        _update_bulk_hold(st, name, report, _now(), extra or [])
    # last_tick은 쓰지 않는다 — 이건 틱이 아니라 진단용 1회 실행이다. 여기서
    # 시각을 남기면 루프가 '방금 틱이 있었다'고 보고 기동 스윕을 건너뛴다.
    # 다음 실행 시각 판단은 상태 DB의 last_sync_at이 하므로 잃는 정보도 없다.
    st.save()
    _write_status(st, auto, names, now)
    # 손으로 띄운 1회 실행이라 사람이 화면 앞에 있다. 루프처럼 대기 중에 키를 볼 수
    # 없으니 한 번만 묻는다 — 답이 없으면(예약 실행 등) 시간이 지나 그냥 끝난다.
    held = _bulk_holds(st, names)
    if held and "--dry-run" not in (extra or []):
        _print_bulk_banner([(n, _bulk_hold(st, n)) for n in held], key_hint=False)
        if keys.ask("  지금 계획을 보고 처리할까요? [y/N]: ",
                    timeout=OFFER_OPEN_TIMEOUT) == "yes":
            _handle_bulk_holds(st, auto, names, extra or [])
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
    # 지난 실행이 남긴 대량 삭제 보류 — 창을 새로 띄웠어도 그 폴더는 여전히 멈춰 있다.
    try:
        start_names = auto_profiles(only)
        start_sync = _last_sync_map(start_names)
        _settle_bulk_holds(st, start_sync)
        held = [n for n in _bulk_holds(st, start_names) if "--dry-run" not in (extra or [])]
        at = _now()
        # 첫 틱이 곧바로 다시 돌릴 폴더(기동 스윕·이미 차례)는 여기서 묻지 않는다 — 그
        # 실행이 끝나면 새 알림이 나오고, 그 알림이 앞서 눌린 키를 버린다. 여기서 물으면
        # 사람이 그 사이에 누른 답이 소리 없이 사라진다(2026-11-11 검토 재현).
        first = decide(st, auto, [n for n in start_names if n in start_sync],
                       now=at, last_sync=start_sync) if held else None
        show = [n for n in held if first is not None and n not in first.names]
        if show:
            _print_bulk_banner(
                [(n, _bulk_hold(st, n)) for n in show],
                recheck={n: _due_at(st, n, start_sync.get(n),
                                    float(auto["min_interval_sec"]), at) for n in show},
                now=at)
    except Exception as exc:   # noqa: BLE001 — 안내 실패가 루프를 막지 않는다
        print(f"    (삭제 보류 안내를 건너뜀 — {type(exc).__name__}: {exc})")

    def _on_signal(_sig, _frm) -> None:
        nonlocal stop
        # 삭제 보류를 묻는 중의 Ctrl+C 는 그 질문에 대한 '아니오'다 — 루프는 계속 돈다.
        if not _sigint_should_stop():
            return
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
            try:
                _idle_wait(st, tick_sec, should_stop=lambda: stop, only=only,
                           extra=extra or [])
            except Exception as exc:   # noqa: BLE001 — 대기가 실패해도 루프는 산다
                _end_idle()
                print(f"[{_now():%H:%M:%S}] 대기 중 오류 — {type(exc).__name__}: {exc}")
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
