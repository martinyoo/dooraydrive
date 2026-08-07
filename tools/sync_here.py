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

성공적으로 동기화한 프로파일의 원격 루트에는 **마커(synchere.bat)를 자동 유지**한다
— 마커는 발견(discovery) 힌트일 뿐이며 sync 엔진에는 보이지 않는다(양축
ALWAYS_EXCLUDE). dry-run에서는 마커도 만들지 않는다.

실행:  python tools\\sync_here.py [--root <폴더>] [dsync sync 추가 인자...]
"""
from __future__ import annotations

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
from dooray_sync.util.paths import to_nfc       # noqa: E402

MARKER = "synchere.bat"


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


def _explain_skip(name: str, info: dict) -> None:
    mode, note = info["mode"], info["note"]
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

    runnable = [(n, i) for n, i in targets if i["mode"] == "sync"]
    for name, info in targets:
        if info["mode"] != "sync":
            _explain_skip(name, info)

    if not runnable:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
