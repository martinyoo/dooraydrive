"""폴더 안 `synchere.bat`의 본체 — 배치파일이 놓인 폴더를 동기화한다.

배치파일은 자기 위치(%~dp0)를 `--root`로 넘긴다. 위치 해석은 두 방향이다:

  상향 — 이 폴더를 **품는** 프로파일이 있으면(local_root 자신 또는 그 하위)
         그 프로파일 하나를 동기화한다. 최장 접두 일치로 구현했다.
  하향 — 품는 프로파일이 없으면 이 폴더 **아래에 있는** 프로파일 전부를 차례로
         동기화한다(WORK처럼 여러 동기화 폴더를 거느린 상위 폴더용).

둘 다 아니면 등록 방법(dsync init 명령)을 경로까지 채워 안내한다. 프로파일 자동
생성은 하지 않는다 — 원격 경로를 추정할 수 없다(실제 구성만 봐도 'WORK/spri 2025'와
최상위 '근무환경'이 공존한다).

sync 실행 여부는 config.toml의 **sync_mode**가 정한다(단일 정본 — 예전의 하드코딩
제외 표는 폐지). 'sync'만 실행하고, 나머지(push/pull/off/미설정)는 사유와 대안을
안내한다. 변경은 `python tools\\set_sync_mode.py`로.

**로컬 마커(프로파일 루트의 synchere.bat)는 등록/해제 스위치다**(2026-08-07 사용자
요구: 복사해 실행하면 등록, 지우면 동기화 대상에서 해제). 정본은 여전히 config다 —
마커는 실행 시점의 판단 근거가 아니라, 관찰될 때 config를 고쳐 쓰는 입력이다.
규칙은 `reconcile_markers` 한 곳에 있다(SYNC.ps1 -Sync도 --check-markers로 호출):

  - sync_mode='sync'인데 루트에 마커 없음 → 자동 해제(off + [synchere-off] 태그).
    프로파일·기준선 DB는 남긴다(soft-delete) — 일시적 부재(GDrive 재동기화·백신
    격리)가 등록 정보를 파괴하지 못하게 하고, 재등록을 더블클릭 한 번으로 만든다.
  - off + [synchere-off] 태그인데 마커 있음(재복사 후 실행) → 자동 재등록(sync 복귀).
  - push/pull/태그 없는 off는 **사람의 결정이므로 절대 자동 전환하지 않는다**
    (더블클릭 한 번이 workenv의 1GB 수신 회피나 writing의 안전 보류를 뒤집으면 안 됨).

성공적으로 동기화한 프로파일의 원격 루트에는 **마커(synchere.bat)를 자동 유지**한다
— 원격 마커는 발견(discovery) 힌트일 뿐이며 sync 엔진에는 보이지 않는다(양축
ALWAYS_EXCLUDE). 해제 시에도 원격 마커는 지우지 않는다(삭제 무전파 + 다른 PC의
발견 힌트 — 정리는 tools/trash_toolfile.py 수동). dry-run에서는 아무것도 쓰지 않는다.

실행:  python tools\\sync_here.py [--root <폴더>] [dsync sync 추가 인자...]
       python tools\\sync_here.py --check-markers [--dry-run]   (정합만, sync 없음)
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dooray_sync.config import config_path      # noqa: E402
from dooray_sync.util.paths import ext_path, to_nfc   # noqa: E402

MARKER = "synchere.bat"
# 자동 해제를 사람의 결정(off)과 구분하는 태그 — sync_note 맨 앞에 붙는다.
# 재등록 자동 복귀는 이 태그가 있는 off에만 적용된다.
AUTO_OFF_PREFIX = "[synchere-off]"


def _norm(p: str | Path) -> str:
    """비교용 절대경로 키: 절대화 + NFC + casefold. 끝 구분자 제거."""
    s = os.path.abspath(str(p))
    return to_nfc(s).casefold().rstrip("\\/")


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + "\\") or child.startswith(parent + "/")


def _load_profiles() -> dict[str, dict]:
    """{프로파일명: {root, mode(None=미설정), note}}. 설정이 없으면 빈 dict."""
    cp = config_path()
    if not os.path.exists(cp):
        return {}
    with open(cp, "rb") as f:
        doc = tomllib.load(f)
    out: dict[str, dict] = {}
    for name, body in (doc.get("profile") or {}).items():
        body = body or {}
        root = str(body.get("local_root") or "").strip()
        if not root:
            continue
        # 키 없음 = 기본 'sync' — load_config의 기본값과 정렬(2026-08-07 사용자 피드백:
        # sync_mode 도입 전에 만들어진 config에서 전부 '미설정 제외'가 떠 혼란).
        # 정책은 PC별 config 소관이므로 기본 sync가 각 PC에서 올바른 해석이다.
        mode = str(body.get("sync_mode") or "sync").strip().lower()
        out[name] = {
            "root": root,
            "mode": mode,
            "note": str(body.get("sync_note") or "").strip(),
            "remote": str(body.get("remote_path") or "").strip(),
            "drive_id": str(body.get("drive_id") or "").strip(),
        }
    return out


def _marker_state(root: str) -> bool | None:
    """루트의 로컬 마커 상태: True(있음) / False(없음) / None(판단 불가).

    os.path.exists는 권한 거부·잠금(백신 스캔 순간)도 False로 뭉갠다 — config.py
    _read_doc이 실측으로 배운 것과 같은 함정이라, 판정 불가를 '삭제 의도'로
    기록하지 않도록 3상으로 구분한다. 루트 폴더 자체가 없으면 False다(존재하지
    않는 폴더를 동기화 대상으로 계속 두는 쪽이 더 위험 — 회귀 테스트 근거)."""
    try:
        os.stat(ext_path(os.path.join(os.path.abspath(str(root)), MARKER)))
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def _ensure_local_marker(root: str) -> None:
    """등록 직후 루트에 로컬 마커를 놓는다 — '등록 = 마커 ON'이 불변식이라,
    --root 직접 호출처럼 마커 없이 등록된 경우 다음 정합이 도로 해제해 버린다.
    실패해도 경고만(등록·동기화 결과에 영향 금지)."""
    try:
        dst = ext_path(os.path.join(os.path.abspath(str(root)), MARKER))
        if not os.path.exists(dst):
            import shutil
            shutil.copyfile(ext_path(REPO / MARKER), dst)
            print(f"       (로컬 마커 배치: {os.path.join(str(root), MARKER)})")
    except OSError as exc:
        print(f"       (로컬 마커 배치 실패 — 무시함: {type(exc).__name__}: {exc})")


def _set_mode(name: str, mode: str, note: str) -> bool:
    """config에 sync_mode/sync_note 기록 — set_sync_mode.py와 같은 왕복(단일 작성자).
    실패는 삼키고 보고만 한다(호출측이 fail-closed 처리)."""
    try:
        from dooray_sync.config import load_config, save_config
        p = load_config(name)
        p.sync_mode = mode
        p.sync_note = note
        save_config(p)
        return True
    except Exception as exc:  # noqa: BLE001 — 정합은 sync 본체를 죽이면 안 된다
        print(f"       (config 기록 실패 — {type(exc).__name__}: {exc})")
        return False


def reconcile_markers(
    profiles: dict[str, dict], *, dry_run: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    """로컬 마커 ↔ config 정합. 마커 = 등록/해제 스위치(모듈 docstring의 규칙).

    profiles의 mode/note를 제자리 갱신하고 (변경 목록[(이름, 새 mode)], 기록 실패
    이름 목록)을 돌려준다. dry_run이면 config를 쓰지 않는다 — 해제는 미리보기의
    일관성을 위해 메모리에서만 제외하고, 재등록은 하지 않는다(기록 없이 sync를
    돌리면 CLI 게이트가 off를 보고 거부하므로 예고만 한다).
    """
    today = datetime.date.today().isoformat()
    changed: list[tuple[str, str]] = []
    failed: list[str] = []
    for name, info in profiles.items():
        state = _marker_state(info["root"])
        if state is None:
            # 판단 불가(권한·잠금)는 해제 근거가 아니다 — config를 바꾸지 않는다.
            print(f"[보류] '{name}' — 마커 상태를 확인할 수 없습니다(권한·잠금). "
                  f"config를 바꾸지 않습니다: {info['root']}")
            continue
        if info["mode"] == "sync" and not state:
            # 주의: 이 note는 SYNC.ps1이 cp949 파이프로 되읽는다 — em dash(—) 등
            # cp949 비인코딩 문자를 넣지 말 것(전역 CLAUDE.md 인코딩 교훈).
            note = (f"{AUTO_OFF_PREFIX} {today} 로컬 {MARKER} 없음 - "
                    f"재등록: 폴더에 {MARKER} 복사 후 실행")
            if dry_run:
                print(f"[해제 예정] '{name}' — 루트에 {MARKER} 없음 "
                      f"(dry-run: 기록 안 함, 이번 미리보기에서도 제외)")
                print(f"       {info['root']}")
                changed.append((name, "off"))
            else:
                print(f"[해제] '{name}' — 루트에 {MARKER} 없음")
                print(f"       {info['root']}")
                if _set_mode(name, "off", note):
                    print(f"       sync_mode=off 기록됨. 재등록: 위 폴더에 {MARKER} "
                          f"복사 후 실행 (기준선 DB는 보존됨)")
                    print(f"       영구 해제로 굳히려면: python tools\\set_sync_mode.py "
                          f"{name} off  (마커가 되살아나도 재등록 안 됨)")
                    changed.append((name, "off"))
                else:
                    print("       기록 실패 — 이번 실행에서는 제외하지만 config에는 "
                          "아직 sync로 남아 있습니다. 다시 실행하면 재시도됩니다.")
                    failed.append(name)
            # 기록 실패·dry-run이어도 이번 실행에서는 제외한다(fail-closed).
            info["mode"], info["note"] = "off", note
            info["_reconcile_reported"] = True
        elif (info["mode"] == "off" and state
              and info["note"].startswith(AUTO_OFF_PREFIX)):
            if dry_run:
                print(f"[재등록 예정] '{name}' — {MARKER} 재확인 (dry-run: 기록 안 함. "
                      f"--dry-run 없이 실행하면 sync가 재개됩니다)")
                info["_pending_reenable"] = True
                info["_reconcile_reported"] = True
                continue
            if _set_mode(name, "sync", f"{today} {MARKER} 재확인으로 자동 재등록"):
                print(f"[재등록] '{name}' — {MARKER} 재확인 → sync_mode=sync")
                info["mode"], info["note"] = "sync", ""
                changed.append((name, "sync"))
            else:
                print(f"[재등록 실패] '{name}' — config 기록 불가, off 유지")
                info["_reconcile_reported"] = True
                failed.append(name)
        # 그 외(push/pull/태그 없는 off, sync+마커 있음)는 사람의 결정 — 손대지 않음
    return changed, failed


def _explain_skip(name: str, info: dict) -> None:
    mode, note = info["mode"], info["note"]
    if mode == "off" and note.startswith(AUTO_OFF_PREFIX):
        print(f"[제외] 프로파일 '{name}' — {MARKER} 삭제로 자동 해제됨 ({note})")
        print(f"       재등록: {info['root']} 에 {MARKER} 복사 후 실행")
        print(f"       영구 해제로 굳히려면: python tools\\set_sync_mode.py {name} off")
        return
    print(f"[제외] 프로파일 '{name}' — sync_mode={mode}"
          + (f" ({note})" if note else ""))
    if mode in ("push", "pull"):
        print(f"       수동 실행: dsync {mode} -p {name}")
    else:
        print(f"       수동 운용: dsync push -p {name} / dsync pull -p {name}")
    print(f"       전환: python tools\\set_sync_mode.py {name} sync")


def _ensure_marker(name: str) -> None:
    """성공한 sync 뒤 원격 루트에 마커가 없으면 올린다 — 실패해도 경고만."""
    try:
        from dooray_sync.api.client import DoorayClient
        from dooray_sync.api.drive import DriveAPI
        from dooray_sync.auth import get_token
        from dooray_sync.config import load_config
        from dooray_sync.core.remote import resolve_remote_root

        p = load_config(name)
        with DoorayClient(p.base_url, get_token()) as client:
            api = DriveAPI(client)
            root_id, _ = resolve_remote_root(api, p.drive_id, p.remote_path)
            if api.find_child_by_name(p.drive_id, root_id, MARKER) is None:
                api.upload_new(p.drive_id, root_id, MARKER, REPO / MARKER)
                print(f"       (원격 마커 복구: {p.remote_path}/{MARKER})")
    except Exception as exc:  # noqa: BLE001 — 마커는 힌트일 뿐, sync 결과에 영향 금지
        print(f"       (원격 마커 확인 실패 — 무시함: {type(exc).__name__}: {exc})")


def _run_sync(name: str, root: str, extra: list[str]) -> int:
    print(f"[프로파일 '{name}'] {root}")
    cmd = [sys.executable, "-m", "dooray_sync.cli.main", "sync", "-p", name, *extra]
    rc = subprocess.call(cmd, cwd=str(REPO))
    if rc == 0 and "--dry-run" not in extra:
        _ensure_marker(name)
    return rc


def main(argv: list[str]) -> int:
    args = list(argv)
    root = os.getcwd()
    if "--root" in args:
        i = args.index("--root")
        if i + 1 >= len(args):
            print("사용법: sync_here.py [--root <폴더>] [추가 인자...]")
            return 2
        root = args[i + 1]
        del args[i:i + 2]
    extra = args

    profiles = _load_profiles()
    if not profiles:
        print("설정 파일이 없습니다. 먼저 설치를 마쳐 주세요 (설치.bat 또는 dsync init).")
        return 2

    # 정합 전용 모드 — SYNC.ps1 -Sync가 실행 전에 호출한다(규칙 구현은 이 파일 한 곳).
    # --emit-modes <파일>: 정합 후의 유효 mode를 "이름\t모드\t재등록예정(0|1)" 줄로
    # UTF-8 기록 — dry-run은 config를 안 쓰므로, SYNC.ps1 -DryRun이 미리보기를
    # 실제 실행과 같은 대상 집합으로 맞추는 유일한 통로다(콘솔 캡처는 cp949 문제).
    if "--check-markers" in extra:
        extra.remove("--check-markers")
        emit = None
        if "--emit-modes" in extra:
            i = extra.index("--emit-modes")
            if i + 1 >= len(extra):
                print("사용법: sync_here.py --check-markers [--dry-run] [--emit-modes <파일>]")
                return 2
            emit = extra[i + 1]
            del extra[i:i + 2]
        _changed, failed = reconcile_markers(profiles, dry_run="--dry-run" in extra)
        if emit:
            with open(emit, "w", encoding="utf-8", newline="\n") as f:
                for n, info in profiles.items():
                    pend = "1" if info.get("_pending_reenable") else "0"
                    f.write(f"{n}\t{info['mode']}\t{pend}\n")
        # 기록 실패 = config와 실제 마커 상태가 어긋난 채 남음 → 호출측이 중단하도록 1.
        return 1 if failed else 0

    base = _norm(root)

    containing: tuple[str, dict] | None = None
    for name, info in profiles.items():
        lkey = _norm(info["root"])
        if _under(base, lkey):
            if containing is None or len(lkey) > len(_norm(containing[1]["root"])):
                containing = (name, info)

    if containing is not None:
        targets = [containing]
    else:
        targets = [(n, i) for n, i in profiles.items() if _under(_norm(i["root"]), base)]

    if not targets:
        # 미등록 폴더 — **형제 유도 자동 등록**을 시도한다(2026-08-07 사용자 요구:
        # "안내가 아니라 그냥 진행"). 같은 부모 폴더 아래 등록된 형제가 있으면
        # 부모의 로컬↔원격 결합이 이미 확정돼 있으므로, 이 폴더의 원격 경로는
        # 추정이 아니라 유도다(예: WORK\spri 2025 → WORK/spri 2025 가 등록돼
        # 있으면 WORK\spri 2024 → WORK/spri 2024). 형제가 없으면 안내로 돌아간다
        # — 임의 위치 자동 등록은 하지 않는다(오결합·거대 폴더 사고 방지).
        abs_root = os.path.abspath(str(root))
        parent_key = _norm(os.path.dirname(abs_root))
        leaf = to_nfc(os.path.basename(abs_root))
        sibling = None
        for _n, info in profiles.items():
            if info["remote"] and _norm(os.path.dirname(info["root"])) == parent_key:
                sibling = info
                break
        if sibling is not None:
            remote_parent = sibling["remote"].replace("\\", "/").rstrip("/").rpartition("/")[0]
            candidate = f"{remote_parent}/{leaf}" if remote_parent else leaf
            pname = "".join(c for c in leaf if c.isalnum() or c in "._-") or "folder"
            n = 1
            base_name = pname
            while pname in profiles:
                n += 1
                pname = f"{base_name}{n}"
            print("미등록 폴더입니다 — 형제 프로파일의 결합에서 유도해 자동 등록합니다:")
            print(f"  로컬  {abs_root}")
            print(f"  원격  {candidate}   (프로파일 '{pname}')")
            print()
            if "--dry-run" in extra:
                print("dry-run — 등록·동기화 없이 계획만 보였습니다. 실행하면 위대로 등록 후 동기화합니다.")
                return 0
            rc = subprocess.call(
                [sys.executable, "-m", "dooray_sync.cli.main", "init", "-p", pname,
                 "--drive-id", sibling["drive_id"], "--local-root", abs_root,
                 "--remote-path", candidate, "--create-remote"],
                cwd=str(REPO))
            if rc != 0:
                print(f"등록 실패(종료코드 {rc}) — 직접 확인이 필요합니다.")
                return rc
            _ensure_local_marker(abs_root)
            print()
            rc = _run_sync(pname, abs_root, extra)
            return rc
        print(f"이 폴더는 동기화 대상이 아닙니다: {root}")
        print()
        print("등록된 동기화 폴더:")
        for name, info in profiles.items():
            mark = "" if info["mode"] == "sync" else f"  (sync 제외: {info['mode']})"
            print(f"  {name:<10} {info['root']}{mark}")
        print()
        print("이 폴더를 새로 등록하려면 (원격 경로는 Dooray 웹에서 확인해 지정):")
        print(f'  dsync init -p <프로파일이름> --local-root "{abs_root}" '
              '--remote-path "<원격/경로>"')
        print("  (원격에 아직 없는 폴더면 --create-remote 를 붙입니다)")
        return 2

    # 실행 전 마커↔config 정합 — 마커가 지워진 대상은 여기서 해제되어 빠지고,
    # 마커를 되살린(태그 off) 대상은 sync로 복귀해 아래 runnable에 잡힌다.
    _changed, marker_failed = reconcile_markers(dict(targets), dry_run="--dry-run" in extra)

    runnable = [(n, i) for n, i in targets if i["mode"] == "sync"]
    for name, info in targets:
        # 정합이 방금 보고한 프로파일은 다시 설명하지 않는다 — dry-run에서
        # '해제 예정' 직후 과거형 '해제됨'이 찍히는 자기모순 방지(적대 검증 지적).
        if info["mode"] != "sync" and not info.get("_reconcile_reported"):
            _explain_skip(name, info)

    if not runnable:
        # 기록 실패는 여기서도 1로 끝낸다 — 2(안내성 오류)로 끝내면 배치의
        # ANY-KEY 재시도 루프가 뜨지 않아, 일시적 config 잠금이라는 재시도가
        # 정확히 유효한 상황에서 재시도가 차단된다(2차 적대 검증 지적).
        if marker_failed:
            print(f"마커 정합 기록 실패: {', '.join(marker_failed)} — config가 잠겨 "
                  f"있었을 수 있습니다. 다시 실행하면 재시도됩니다.")
            return 1
        # dry-run에서 재등록 예정뿐인 경우: 실제 실행은 sync가 재개되므로
        # 안내성 오류(2)가 아니라 0으로 끝낸다(dry-run과 실제의 종료 의미 정렬).
        if any(i.get("_pending_reenable") for _, i in targets):
            return 0
        return 2

    if len(runnable) > 1:
        print(f"이 폴더 아래 동기화 대상 {len(runnable)}개를 차례로 동기화합니다: "
              + ", ".join(n for n, _ in runnable))
        print()

    failed: list[str] = []
    for name, info in runnable:
        rc = _run_sync(name, info["root"], extra)
        if rc != 0:
            failed.append(name)
        print()

    if failed:
        print(f"실패한 프로파일: {', '.join(failed)} — 다시 실행하면 실패분만 재시도됩니다.")
        return 1
    if marker_failed:
        # 화면·메모리는 '해제'인데 config는 sync로 남은 상태 — 성공(0)으로 닫으면
        # 어긋남이 조용히 굳는다. rc=1이면 배치의 재시도 루프가 일시적 잠금에 대응.
        print(f"마커 정합 기록 실패: {', '.join(marker_failed)} — config가 잠겨 "
              f"있었을 수 있습니다. 다시 실행하면 재시도됩니다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
