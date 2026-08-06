"""폴더 안 `synchere.bat`의 본체 — 배치파일이 놓인 폴더를 동기화한다.

배치파일은 자기 위치(%~dp0)를 `--root`로 넘긴다. 위치 해석은 두 방향이다:

  상향 — 이 폴더를 **품는** 프로파일이 있으면(local_root 자신 또는 그 하위)
         그 프로파일 하나를 동기화한다. "한 단계씩 위로 올라가며 등록된 최상위
         폴더를 찾는" 동작과 동치이며, 최장 접두 일치로 구현했다.
  하향 — 품는 프로파일이 없으면 이 폴더 **아래에 있는** 프로파일 전부를 차례로
         동기화한다. WORK처럼 여러 동기화 폴더를 거느린 상위 폴더에 배치파일을
         두는 경우다. 제외 프로파일은 사유를 표시하고 건너뛴다.

둘 다 아니면 등록 방법(dsync init 명령)을 경로까지 채워 안내한다.
프로파일 자동 생성은 하지 않는다 — 원격 경로를 추정할 수 없다(실제 구성만 봐도
'WORK/spri 2025'와 최상위 '근무환경'이 공존한다). 잘못 추정해 빈 원격 폴더를
만들면 pull 0건이 정상처럼 보이는 함정이 생긴다.

`synchere.bat` 자신은 어느 쪽으로도 동기화되지 않는다(스캐너·원격 수집기 양축의
ALWAYS_EXCLUDE — dooray_sync/core/scanner.py).

실행:  python tools\\sync_here.py [--root <폴더>] [dsync sync 추가 인자...]
       --root 생략 시 현재 폴더(CWD) 기준.
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

# sync 실행에서 제외하는 프로파일과 그 사유. 제외를 풀려면 이 표에서 지우고,
# 같은 표가 SYNC.ps1(-Sync 모드)에도 있으므로 함께 고친다.
SYNC_EXCLUDED: dict[str, tuple[str, str]] = {
    # 프로파일: (사유, 대안 안내)
    "workenv": (
        "원격 전용 336건(약 1GB)을 받지 않기로 결정(2026-08-07) — push 전용 운용",
        "올리기만 하려면:  dsync push -p workenv",
    ),
    "study": (
        "sync 전환 미검토 — push/pull로만 운용",
        "올리기: dsync push -p study / 받기: dsync pull -p study",
    ),
    "writing": (
        "보류 4건 정리 전 sync 금지 — sync가 이를 충돌보존으로 판정해 로컬 원본을 개명한다",
        "정리하려면 원격과 내용 대조 후 결정:  dsync reconcile -p writing",
    ),
}


def _norm(p: str | Path) -> str:
    """비교용 절대경로 키: 절대화 + NFC + casefold. 끝 구분자 제거."""
    s = os.path.abspath(str(p))
    return to_nfc(s).casefold().rstrip("\\/")


def _under(child: str, parent: str) -> bool:
    """child가 parent 자신이거나 그 하위인가 (정규화 키 기준)."""
    return child == parent or child.startswith(parent + "\\") or child.startswith(parent + "/")


def _load_profiles() -> dict[str, str]:
    """{프로파일명: local_root}. 설정이 없으면 빈 dict."""
    cp = config_path()
    if not os.path.exists(cp):
        return {}
    with open(cp, "rb") as f:
        doc = tomllib.load(f)
    out: dict[str, str] = {}
    for name, body in (doc.get("profile") or {}).items():
        root = str((body or {}).get("local_root") or "").strip()
        if root:
            out[name] = root
    return out


def _run_sync(name: str, lroot: str, extra: list[str]) -> int:
    print(f"[프로파일 '{name}'] {lroot}")
    cmd = [sys.executable, "-m", "dooray_sync.cli.main", "sync", "-p", name, *extra]
    return subprocess.call(cmd, cwd=str(REPO))


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
    extra = args                      # --dry-run, --full 등은 그대로 전달

    profiles = _load_profiles()
    if not profiles:
        print("설정 파일이 없습니다. 먼저 설치를 마쳐 주세요 (설치.bat 또는 dsync init).")
        return 2

    base = _norm(root)

    # 상향: 이 폴더를 품는 프로파일 (가장 구체적인 것 하나)
    containing: tuple[str, str] | None = None
    for name, lroot in profiles.items():
        lkey = _norm(lroot)
        if _under(base, lkey):
            if containing is None or len(lkey) > len(_norm(containing[1])):
                containing = (name, lroot)

    # 하향: 품는 프로파일이 없으면 이 폴더 아래의 프로파일 전부 (WORK 등 상위 폴더)
    if containing is not None:
        targets = [containing]
    else:
        targets = [(n, r) for n, r in profiles.items() if _under(_norm(r), base)]

    if not targets:
        print(f"이 폴더는 동기화 대상이 아닙니다: {root}")
        print()
        print("등록된 동기화 폴더:")
        for name, lroot in profiles.items():
            mark = "  (sync 제외)" if name in SYNC_EXCLUDED else ""
            print(f"  {name:<10} {lroot}{mark}")
        print()
        print("이 폴더를 새로 등록하려면 (원격 경로는 Dooray 웹에서 확인해 지정):")
        print(f'  dsync init -p <프로파일이름> --local-root "{os.path.abspath(str(root))}" '
              '--remote-path "<원격/경로>"')
        print("  (원격에 아직 없는 폴더면 --create-remote 를 붙입니다)")
        return 2

    runnable = [(n, r) for n, r in targets if n not in SYNC_EXCLUDED]
    for name, _lroot in targets:
        if name in SYNC_EXCLUDED:
            why, alt = SYNC_EXCLUDED[name]
            print(f"[제외] 프로파일 '{name}' — {why}")
            print(f"       {alt}")

    if not runnable:
        print("       (제외를 풀려면 tools/sync_here.py 와 SYNC.ps1 의 제외 표에서 지우세요)")
        return 2

    if len(runnable) > 1:
        print(f"이 폴더 아래 동기화 대상 {len(runnable)}개를 차례로 동기화합니다: "
              + ", ".join(n for n, _ in runnable))
        print()

    failed: list[str] = []
    for name, lroot in runnable:
        rc = _run_sync(name, lroot, extra)
        if rc != 0:
            failed.append(name)
        print()

    if failed:
        print(f"실패한 프로파일: {', '.join(failed)} — 다시 실행하면 실패분만 재시도됩니다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
