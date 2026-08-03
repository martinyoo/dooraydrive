"""M2 실계정 검증 8~11단계 자동 실행 — [docs/M2_실계정검증.md] 대응.

원격을 **쓰는** 스크립트다(폴더 개명, 파일 휴지통 이동). 그래서 맨 앞에 가드를 둔다:
시험용 프로파일(원격 접두 `_m2_test`, 로컬 루트가 `.dbg\\m2` 아래)이 아니면 즉시 중단한다.
가드를 통과하지 못하면 API를 한 번도 호출하지 않는다.

실행:
    python tools\\m2_scenario.py                # 8~10단계 실행·검증 (정리 안 함)
    python tools\\m2_scenario.py --cleanup      # 8~10단계 후 _m2_test 를 휴지통으로
    python tools\\m2_scenario.py --cleanup-only # 검증은 건너뛰고 정리만
    python tools\\m2_scenario.py --skip-crash   # 크래시 단계(대용량 전송) 생략

재실행 안전성: 8단계는 이미 개명돼 있으면 되돌리지 않고 [미검증]으로 건너뛴다(교훈 §16).

되돌릴 수 있는 것만 한다 — 삭제는 전부 휴지통이고 영구삭제 API는 쓰지 않는다.

**판정 규칙 (적대 검증에서 초판이 통째로 지적당한 부분)**
CLI의 계획 요약은 `_kv` 표라 **라벨을 값과 무관하게 항상 출력한다.** 따라서
`"변경 없음" in out` 같은 부분문자열 검사는 **실패할 수 없다** — 전 파일이 재업로드되는
상태에서도 참이다. 그래서 이 스크립트는 라벨 존재가 아니라 **값**을 본다(교훈 §5).
'무동작'은 계획 절에 단독으로 찍히는 `  변경 없음` 줄 + 전송량 0 + 삭제 0으로만 인정한다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dooray_sync.api.client import DoorayApiError, DoorayClient   # noqa: E402
from dooray_sync.api.drive import NO_ACCESS_AUTHORITY, DriveAPI   # noqa: E402
from dooray_sync.auth import get_token                   # noqa: E402
from dooray_sync.config import db_path, load_config      # noqa: E402
from dooray_sync.core.remote import resolve_remote_root  # noqa: E402
from dooray_sync.store.db import Store                   # noqa: E402
from dooray_sync.util.paths import ext_path              # noqa: E402

MAIN = "dooray_sync.cli.main"
A, B = "m2test", "m2peer"           # A = 검증 대상, B = '다른 PC' 역할

_results: list[tuple[bool, str, str]] = []
_skipped: list[tuple[str, str]] = []


# ---------------------------------------------------------------- 출력
def section(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def step(title: str) -> None:
    print()
    print(f"── {title}")


def check(ok: bool, msg: str, detail: str = "") -> bool:
    _results.append((ok, msg, detail))
    print(f"   {'[통과]' if ok else '[실패]'} {msg}" + (f" — {detail}" if detail else ""))
    return ok


def skip(msg: str, why: str) -> None:
    """검증하지 못한 것. **통과로 세지 않는다.**"""
    _skipped.append((msg, why))
    print(f"   [미검증] {msg} — {why}")


def die(msg: str) -> None:
    print()
    print(f"!! 중단: {msg}")
    raise SystemExit(2)


# ---------------------------------------------------------------- CLI
def cli(*args: str) -> str:
    cmd = [sys.executable, "-m", MAIN, *args]
    print(f"   $ python -m {MAIN} {' '.join(args)}")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        print(f"     {line}")
    return out


# ---------------------------------------------------------------- 출력 파싱
def plan_value(out: str, label: str) -> str:
    """계획 요약의 `라벨 : 값` 에서 값만. 없으면 ''.

    `_kv`는 `  라벨   : 값` 형태이므로 콜론이 있는 줄만 본다.
    """
    for line in out.splitlines():
        if label in line and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def count_of(out: str, label: str) -> int:
    """`라벨 : N건` 의 N. 라벨을 못 찾으면 -1(= 알 수 없음)."""
    v = plan_value(out, label)
    if not v:
        return -1
    digits = "".join(c for c in v.split("건")[0] if c.isdigit())
    return int(digits) if digits else -1


def deletes_in(out: str) -> int:
    return count_of(out, "실제로 사라질 항목")


def is_no_op(out: str) -> bool:
    """정말 아무 일도 계획되지 않았는가.

    요약표의 `변경 없음 : N건`(stats.unchanged)은 **어떤 상태에서도 출력된다.**
    무동작의 유일한 신호는 계획 절에 단독으로 찍히는 `  변경 없음` 줄이다.
    거기에 전송량·삭제 0을 교차검증으로 붙인다(교훈 §13: 숫자로 대조한다).
    """
    lines = [ln.rstrip() for ln in out.splitlines()]
    marker = ("  변경 없음" in lines) or ("변경 없음." in lines)
    up = plan_value(out, "올릴 용량")
    down = plan_value(out, "받을 용량")
    return marker and up == "0B" and down == "0B" and deletes_in(out) == 0


def has_report_for(out: str, name: str) -> bool:
    """`보고: <경로> — <사유>` 상세 줄에 그 파일이 있는가(라벨 '보고만'과 구분)."""
    return any(ln.strip().startswith("보고: ") and name in ln for ln in out.splitlines())


# ---------------------------------------------------------------- 가드
def guard():
    section("0. 안전 가드 — 시험용 프로파일인지 먼저 확인한다")
    for var in ("DOORAY_SYNC_CONFIG_DIR", "DOORAY_SYNC_STATE_DIR"):
        val = os.environ.get(var, "")
        print(f"   {var} = {val or '(미설정)'}")
        if not val:
            die(f"{var} 가 설정되지 않았습니다. 격리된 PowerShell 창에서 실행하세요 "
                "— 실업무 설정을 읽을 위험이 있어 진행하지 않습니다.")
        if ".dbg" not in val.replace("/", "\\").lower():
            die(f"{var} 가 .dbg 아래가 아닙니다: {val}")
    try:
        pa, pb = load_config(A), load_config(B)
    except FileNotFoundError as exc:
        die(f"프로파일을 찾을 수 없습니다({exc}).")
    for p in (pa, pb):
        print(f"   {p.name}: remote_path={p.remote_path!r} local_root={p.local_root}")
        if p.remote_path.strip("/").lower() != "_m2_test":
            die(f"[{p.name}] 원격 접두가 '_m2_test'가 아닙니다 — 실데이터 프로파일로 보입니다.")
        if ".dbg\\m2" not in p.local_root.replace("/", "\\").lower():
            die(f"[{p.name}] 로컬 루트가 .dbg\\m2 아래가 아닙니다 — 실데이터 폴더로 보입니다.")
        if not p.drive_id:
            die(f"[{p.name}] drive_id가 비어 있습니다.")
    check(True, "두 프로파일 모두 시험용(_m2_test / .dbg\\m2)임을 확인")
    return pa, pb


def remote_id(profile: str, rel: str) -> str | None:
    with Store(db_path(profile)) as st:
        p = load_config(profile)
        rec = st.get_by_path(p.drive_id, rel)
        return rec.file_id if rec else None


# ---------------------------------------------------------------- 8단계
def step8(pa, drive: DriveAPI) -> None:
    section("8단계. 폴더 개명 — 대량 오삭제가 없는가 (R8 / B4)")

    fid = remote_id(A, "하위")
    if not fid:
        # 이미 한 번 돌린 뒤 재실행한 경우다. 되돌려 놓고 다시 하는 것은 파괴적이므로
        # (교훈 §16) 그냥 건너뛴다.
        if remote_id(A, "하위2"):
            skip("8단계 폴더 개명", "이미 '하위2'로 개명돼 있음(재실행) — 되돌리지 않고 건너뜀")
            return
        die("'하위' 폴더의 원격 id를 DB에서 찾지 못했습니다.")
    step("원격에서 '하위' → '하위2' 로 개명 (API)")
    print(f"   file_id={fid}")
    drive.rename(pa.drive_id, fid, "하위2")
    print("   개명 완료  ※ 실패 시 되돌리기: Dooray 웹에서 '하위2'를 '하위'로 되돌리면 된다")

    step("계획 확인 (삭제가 0건이어야 한다)")
    out = cli("sync", "-p", A, "--dry-run")
    n = deletes_in(out)
    check(n == 0, "폴더 개명이 삭제로 오인되지 않음", f"실제로 사라질 항목 {n}건")
    relisted = count_of(out, "하위 재열람")
    check(relisted >= 1, "하위 트리 재열람이 실제로 일어남(B4)", f"{relisted}건")

    step("실행")
    cli("sync", "-p", A)

    local = Path(pa.local_root)
    check(os.path.isdir(ext_path(local / "하위2")), "로컬 폴더가 '하위2'로 반영됨")
    check(os.path.isfile(ext_path(local / "하위2" / "a.txt")), "하위 파일 a.txt 가 그대로 남아 있음")

    step("재실행이 조용한가 (2패스)")
    out2 = cli("sync", "-p", A, "--dry-run")
    check(is_no_op(out2), "개명 처리 후 재실행이 무동작",
          f"올림={plan_value(out2, '올릴 용량')} 받음={plan_value(out2, '받을 용량')} "
          f"삭제={deletes_in(out2)}건")


# ---------------------------------------------------------------- 9단계
def step9(pa, drive: DriveAPI) -> None:
    section("9단계. 삭제 전파 — 되돌릴 수 있는 형태로만")

    target = "로컬.txt"
    fid = remote_id(A, target)
    if not fid:
        # 이 단계가 이미 성공하면 레코드가 정리되고 로컬 파일도 사라진다(재실행).
        if not os.path.exists(ext_path(Path(pa.local_root) / target)):
            skip("9단계 삭제 전파", "이미 처리됨(로컬·기록 모두 정리된 상태) — 건너뜀")
            return
        die(f"'{target}'의 원격 id를 찾지 못했습니다.")

    step(f"원격에서 '{target}' 를 휴지통으로 (API, 영구삭제 아님)")
    try:
        drive.move_to_trash(pa.drive_id, fid)
        print("   휴지통 이동 완료  ※ 되돌리기: Dooray 웹 휴지통에서 복원")
    except DoorayApiError as exc:
        # 실측: 이미 휴지통에 있는 항목의 move는 HTTP 200 + resultCode=-15700100로 실패한다.
        # 재실행이면 이미 들어가 있는 상태이므로 '이미 처리됨'으로 관용한다(drive.py 주석).
        if exc.result_code != NO_ACCESS_AUTHORITY:
            raise
        print("   이미 휴지통에 있습니다(재실행) — 그대로 진행합니다")

    step("전파 없이 보면 '보고만' 이어야 한다")
    out = cli("sync", "-p", A, "--dry-run")
    check(deletes_in(out) == 0, "삭제 전파가 꺼진 기본값에서는 지우지 않음",
          f"{deletes_in(out)}건")
    check(has_report_for(out, target), f"원격 삭제를 '{target}' 보고로 알림")

    step("전파를 켜고 계획 확인")
    out = cli("sync", "-p", A, "--propagate-deletes", "--dry-run")
    aborted = "대량 삭제로 판단해" in out
    if aborted:
        # 시험 프로파일은 파일이 몇 개 안 돼 절대 건수 임계에는 안 걸리지만,
        # 기준선이 작으면 비율 임계에 걸릴 수 있다. 게이트가 **실행 전에** 막았다는 것
        # 자체가 검증 대상이므로 통과로 센다. 그다음 명시적으로 허용해 실제 삭제를 본다.
        check(True, "대량삭제 게이트가 실행 전에 차단함(안전장치 동작)")
        check("아무것도 실행하지 않고" in out, "부분 실행 없이 전체 중단임을 명시")
        step("--allow-bulk-delete 로 명시 허용 후 재확인")
        out = cli("sync", "-p", A, "--propagate-deletes", "--allow-bulk-delete", "--dry-run")

    n = deletes_in(out)
    ok = check(n == 1, "삭제 대상이 정확히 1건", f"{n}건")
    ok &= check(target in out, f"삭제 대상이 {target} 임")
    if not ok:
        die("삭제 계획이 예상과 달라 실행하지 않습니다(사용자 확인 필요).")

    step("실행")
    extra = ["--allow-bulk-delete"] if aborted else []
    cli("sync", "-p", A, "--propagate-deletes", *extra)
    gone = not os.path.exists(ext_path(Path(pa.local_root) / target))
    check(gone, "로컬 파일이 사라짐(휴지통으로 이동)")
    print("   ※ Windows 휴지통에 있는지 직접 한 번 확인해 주세요(영구삭제가 아님의 실증).")


# ---------------------------------------------------------------- 10단계
def _part_files(root: Path) -> list[Path]:
    return list(root.rglob(".dooraysync_tmp/*.part"))


def step10(pa, pb) -> None:
    section("10단계. 크래시 복구 — 전송 중 강제 종료 후 무손실 수렴")

    big_name = "대용량.bin"
    big = Path(pb.local_root) / big_name
    size = 40 * 1024 * 1024
    step(f"'다른 PC'({B})에서 40MB 파일을 만들어 올린다")
    with open(ext_path(big), "wb") as f:
        f.write(os.urandom(size))
    cli("sync", "-p", B)

    step(f"{A} 가 받는 도중 프로세스를 강제 종료한다")
    local = Path(pa.local_root)
    log_path = Path(pa.local_root).parent / "crash_run.log"
    injected = False
    with open(ext_path(log_path), "w", encoding="utf-8") as logf:
        proc = subprocess.Popen([sys.executable, "-m", MAIN, "sync", "-p", A],
                                cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
        # 임시파일(.part)이 생기는 순간이 '전송이 실제로 시작됐다'는 신호다.
        # 시간으로 어림잡지 않고 그 신호를 기다린다.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if _part_files(local):
                proc.kill()
                proc.wait(timeout=30)
                injected = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        else:
            proc.kill()
            proc.wait(timeout=30)

    if not injected:
        # 크래시를 못 일으켰으면 이 단계는 **검증한 것이 없다.** 통과로 세지 않는다.
        skip("전송 중 강제 종료 주입", f"전송 시작 신호(.part)를 잡지 못함 — 로그: {log_path}")
        skip("status가 미완료 저널을 보고", "크래시가 주입되지 않아 판정 불가")
        skip("복구 절이 출력됨", "크래시가 주입되지 않아 판정 불가")
    else:
        print("   강제 종료됨(.part 관측 후)")
        check(True, "전송 중 강제 종료 주입")

        out = cli("status", "-p", A)
        n_j = count_of(out, "미완료 저널")
        check(n_j >= 1, "status가 미완료 저널을 보고", f"{n_j}건")

        step("재실행 — 복구 후 정상 수렴해야 한다")
        out = cli("sync", "-p", A)
        check("중단된 작업 복구" in out, "복구 절이 출력됨")

    dest = local / big_name
    actual = os.path.getsize(ext_path(dest)) if os.path.exists(ext_path(dest)) else -1
    check(actual == size, "재실행 후 파일이 온전한 크기로 존재", f"{actual}B / 기대 {size}B")

    left = _part_files(local)
    check(not left, "임시파일(.part) 찌꺼기 없음", f"{len(left)}개")

    out = cli("sync", "-p", A, "--dry-run")
    check(is_no_op(out), "복구 후 재실행이 무동작",
          f"올림={plan_value(out, '올릴 용량')} 받음={plan_value(out, '받을 용량')}")


# ---------------------------------------------------------------- 11단계
def cleanup(pa, drive: DriveAPI) -> None:
    section("11단계. 정리 — 원격 시험 폴더를 휴지통으로")
    fid, pref = resolve_remote_root(drive, pa.drive_id, pa.remote_path)
    if pref.strip("/").lower() != "_m2_test":
        die(f"정리 대상이 _m2_test 가 아닙니다: {pref!r}")
    if not fid:
        die("정리 대상 폴더 id를 확인하지 못했습니다.")
    print(f"   대상: {pref} (file_id={fid})")
    drive.move_to_trash(pa.drive_id, fid)
    check(True, f"원격 '{pref}' 를 휴지통으로 이동(복구 가능)")
    print("   로컬 .dbg\\m2 폴더와 PowerShell 창은 직접 정리하세요.")


# ---------------------------------------------------------------- main
def main() -> int:
    args = set(sys.argv[1:])
    pa, pb = guard()
    only_cleanup = "--cleanup-only" in args

    with DoorayClient(pa.base_url, get_token()) as client:
        drive = DriveAPI(client)
        if not only_cleanup:
            step8(pa, drive)
            step9(pa, drive)
            if "--skip-crash" not in args:
                step10(pa, pb)
            else:
                print("\n(크래시 단계 생략)")
        if "--cleanup" in args or only_cleanup:
            cleanup(pa, drive)

    section("결과 요약")
    for ok, msg, detail in _results:
        print(f"  {'[통과]' if ok else '[실패]'} {msg}" + (f" — {detail}" if detail else ""))
    for msg, why in _skipped:
        print(f"  [미검증] {msg} — {why}")

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [m for ok, m, _ in _results if not ok]
    print()
    print(f"  통과 {passed} / 판정 {len(_results)}   미검증 {len(_skipped)}")
    if failed:
        print()
        print("  실패 항목:")
        for m in failed:
            print(f"    - {m}")
    if _skipped:
        print()
        print("  ※ 미검증 항목은 '통과'가 아닙니다 — 그 명제는 확인되지 않았습니다.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
