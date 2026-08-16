"""`synchere.bat --auto <동사>`의 본체 — 단일 접점의 자동 동기화 창구.

동사: on | off | status | now | loop

**종료코드는 0 또는 2뿐이다.** 1을 내면 synchere.bat이 "실패 — 아무 키나 누르면
재시도" 루프(최대 30회)를 띄우는데, 설정 조작에는 그 재시도가 아무 의미가 없다.
그래서 실패도 안내(2)로 끝낸다.

config를 쓰는 것은 on/off뿐이고 status/now/loop는 읽기 전용이다.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

from ..config import (
    AUTO_DEFAULTS,
    Profile,
    config_path,
    load_auto,
    load_config,
    save_auto,
    save_config,
)
from .gate import check_profile
from .state import AutoState, auto_dir

__all__ = ["dispatch", "VERBS"]

VERBS = ("on", "off", "status", "now", "loop")

EXIT_OK = 0
EXIT_GUIDE = 2


def _out(msg: str = "") -> None:
    print(msg)


def _profiles_from(profiles: dict[str, dict], targets: list[tuple[str, dict]] | None,
                   all_: bool) -> list[str]:
    """동사가 적용될 프로파일 이름 목록. --all이면 config 전체, 아니면 이 폴더."""
    if all_:
        return sorted(profiles, key=str.casefold)
    return [name for name, _info in (targets or [])]


def _turn_on(names: list[str]) -> int:
    """편입 게이트를 통과한 프로파일만 auto_sync=true로. fail-closed."""
    if not names:
        _out("이 폴더에 해당하는 동기화 프로파일이 없습니다.")
        _out("먼저 이 폴더에서 synchere.bat을 한 번 실행해 등록하세요.")
        return EXIT_GUIDE

    accepted: list[str] = []
    for name in names:
        try:
            p = load_config(name)
        except (FileNotFoundError, ValueError) as exc:
            _out(f"[거부] {name} — {exc}")
            continue
        r = check_profile(p)
        if not r.ok:
            _out(f"[거부] {name}")
            for why in r.reasons:
                _out(f"        {why}")
            continue
        for note in r.notes:
            _out(f"[참고] {name} — {note}")
        if p.auto_sync:
            _out(f"[유지] {name} — 이미 자동 대상입니다")
            accepted.append(name)
            continue
        p.auto_sync = True
        save_config(p)
        _out(f"[등록] {name} — 자동 동기화 대상")
        accepted.append(name)

    if not accepted:
        _out("")
        _out("자동 동기화를 켜지 못했습니다. 위 사유를 먼저 해소하세요.")
        return EXIT_GUIDE

    # 퇴근 시각의 기준이 되는 근무시간을 확인받는다. 이미 값이 있으면 묻지 않는다 —
    # 매번 물으면 --auto on이 재실행하기 부담스러운 명령이 된다.
    _ensure_work_hours()

    from .launcher import install, launcher_path
    ok, why = install()
    _out("")
    if ok:
        _out(f"[시작프로그램] 등록됨 — {launcher_path()}")
        _out("  다음 로그온부터 창이 자동으로 뜹니다(최소화).")
    else:
        _out(f"[시작프로그램] 등록 실패 — {why}")
        _out("  자동 기동 없이도 'synchere.bat --auto loop'로 직접 띄울 수 있습니다.")

    auto = load_auto()
    _out("")
    _out(f"자동 대상 {len(accepted)}개: {', '.join(accepted)}")
    _out(f"  근무시간 {auto['work_hours']}시간 → 퇴근 스윕은 기동 "
         f"{auto['work_hours']}시간 뒤의 15분 전")
    _out(f"  평시 주기 {auto['min_interval_sec'] // 60}분 · 틱 {auto['tick_sec']}초")
    _out("")
    _out("지금 바로 시작하려면: synchere.bat --auto loop")
    return EXIT_OK


def _ensure_work_hours() -> None:
    """[auto] work_hours가 없으면 한 번 물어 저장한다. 대화 불가 환경이면 기본값.

    이미 값이 있으면 묻지 않는다 — 매번 물으면 `--auto on`이 재실행하기 부담스러운
    명령이 되고, 그러면 편입 게이트를 다시 태우는 정상 경로가 막힌다.
    """
    if "work_hours" in _raw_auto_keys():
        return
    default = float(AUTO_DEFAULTS["work_hours"])
    value = default
    if sys.stdin is not None and sys.stdin.isatty():
        _out("")
        _out("PC를 켜고 몇 시간 뒤에 퇴근하시나요? (퇴근 15분 전에 한 번 몰아서 동기화합니다)")
        try:
            raw = input(f"  근무시간 [{default:g}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            # isatty()가 True인데 stdin이 EOF인 환경이 실재한다(자동화·리다이렉트).
            # 여기서 죽으면 --auto on이 프로파일만 켜 놓고 런처를 못 만든 반쪽
            # 상태로 끝난다 — 기본값으로 계속 간다.
            _out("")
            raw = ""
        if raw:
            try:
                value = float(raw)
            except ValueError:
                _out(f"  숫자가 아니라 기본값 {default:g}시간으로 둡니다.")
                value = default
    if not (0 < value <= 24):
        value = default
    save_auto({"work_hours": value})
    _out(f"  근무시간 {value:g}시간으로 저장했습니다(변경: config.toml의 [auto] work_hours).")


def _raw_auto_keys() -> set[str]:
    """config에 실제로 적혀 있는 [auto] 키 — 기본값과 구분하기 위해 원문을 본다."""
    import tomllib
    try:
        with open(config_path(), "rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError):
        return set()
    raw = doc.get("auto")
    return set(raw) if isinstance(raw, dict) else set()


def _turn_off(names: list[str], all_: bool) -> int:
    changed: list[str] = []
    for name in names:
        try:
            p = load_config(name)
        except (FileNotFoundError, ValueError):
            continue
        if not p.auto_sync:
            continue
        p.auto_sync = False
        save_config(p)
        changed.append(name)

    if changed:
        _out(f"[해제] 자동 대상에서 제외: {', '.join(changed)}")
    else:
        _out("자동 대상이던 프로파일이 없습니다.")

    if all_:
        from .launcher import remove
        ok, why = remove()
        _out(f"[시작프로그램] {'제거됨' if ok else '제거 실패 — ' + why}")
        _out("  도는 창이 있으면 닫아 주세요(다음 로그온부터는 뜨지 않습니다).")
    else:
        _out("  시작프로그램 등록은 그대로입니다 — 전부 끄려면 --auto off --all")
    return EXIT_OK


def _status(names: list[str], all_: bool) -> int:
    """읽기 전용. **네트워크 호출 0건**을 보장한다."""
    from .launcher import describe as describe_launcher

    auto = load_auto()
    st = AutoState()

    _out("== 자동 동기화 상태")
    reg, detail = describe_launcher()
    _out(f"  시작프로그램 : {detail}")
    _out(f"  상태 폴더    : {auto_dir()}")
    _out(f"  근무시간     : {auto['work_hours']:g}시간 "
         f"(퇴근 스윕 = 기동 + {auto['work_hours']:g}h - 15분)")
    _out(f"  평시 주기    : {auto['min_interval_sec'] // 60}분 · 틱 {auto['tick_sec']}초")
    _out(f"  무인 삭제    : {auto['max_auto_deletes']}건 / "
         f"{auto['max_auto_delete_mb']}MB 이하만 자동")

    _out("")
    if st.last_tick:
        _out(f"  마지막 틱    : {st.last_tick}")
    else:
        _out("  마지막 틱    : (아직 없음 — 창을 한 번도 안 띄웠습니다)")
    if st.day_start:
        _out(f"  오늘 기동    : {st.day_start}")
        eod = _eod_at(st.day_start, float(auto["work_hours"]))
        if eod:
            done = st.last_eod_date == _dt.date.today().isoformat()
            _out(f"  퇴근 스윕    : {eod:%H:%M} " + ("(완료)" if done else "(예정)"))
    _out("")

    rows: list[tuple[str, str]] = []
    for name in names:
        try:
            p = load_config(name)
        except (FileNotFoundError, ValueError):
            continue
        if p.auto_sync:
            r = check_profile(p)
            mark = "자동" if r.ok else "자동(주의)"
            extra = "" if r.ok else f" — {r.reasons[0]}"
            if p.sync_mode != "sync":
                extra += f"  [sync_mode={p.sync_mode} — 실제로는 돌지 않습니다]"
            mult = st.backoff_mult(name)
            if mult > 1.0:
                extra += f"  [주기 x{mult:g}]"
            last = _last_outcome(name)
            if last:
                extra += f"  마지막 {last}"
        else:
            mark = "수동"
            extra = ""
        rows.append((name, f"{mark}{extra}"))

    if rows:
        width = max(len(n) for n, _ in rows)
        for name, note in rows:
            _out(f"  {name.ljust(width)}  {note}")
    else:
        _out("  (해당 프로파일 없음)")

    # 통지 — 자동 실행이 사람 손을 기다리는 것들.
    from . import notify
    block = notify.format_block()
    if block:
        _out("")
        _out(block)

    if not all_:
        _out("")
        _out("  전체를 보려면: synchere.bat --auto status --all")
    return EXIT_OK


def _last_outcome(name: str) -> str:
    """자식이 남긴 마지막 보고의 결과. 창이 닫혀 있어도 답할 수 있는 근거다."""
    import json

    from ..util.paths import ext_path
    try:
        with open(ext_path(auto_dir() / "last" / f"{name}.json"), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    outcome = str(data.get("outcome") or "")
    when = str(data.get("finished_at") or "")[5:16].replace("T", " ")
    return f"{outcome}({when})" if outcome else ""


def _eod_at(day_start_iso: str, work_hours: float) -> _dt.datetime | None:
    try:
        start = _dt.datetime.fromisoformat(day_start_iso)
    except ValueError:
        return None
    return start + _dt.timedelta(hours=work_hours) - _dt.timedelta(minutes=15)


def dispatch(verb: str, *, profiles: dict[str, dict],
             targets: list[tuple[str, dict]] | None, all_: bool,
             extra: list[str]) -> int:
    """sync_here.py가 부르는 진입점. 반환은 0 또는 2뿐."""
    if verb not in VERBS:
        _out(f"알 수 없는 --auto 동사: {verb!r}")
        _out(f"  쓸 수 있는 것: {' | '.join(VERBS)}")
        return EXIT_GUIDE

    names = _profiles_from(profiles, targets, all_)

    if verb == "on":
        return _turn_on(names)
    if verb == "off":
        return _turn_off(names, all_)
    if verb == "status":
        return _status(names, all_)

    # now / loop — 러너로 넘긴다(자식 실행이 있으므로 네트워크를 쓴다).
    from .runner import run_loop, run_once
    only = None if all_ else [n for n in names]
    if verb == "now":
        return run_once(only=only, extra=extra)
    return run_loop(only=only, extra=extra)
