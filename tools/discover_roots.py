"""드라이브에서 동기화 루트 마커(synchere.bat)를 찾아 나열한다 — 새 PC 부트스트랩용.

정본 관계: 마커는 **발견 힌트**일 뿐이다. 여기서 프로파일을 자동 생성하지 않는다 —
이미 등록된 폴더는 프로파일명을, 미등록 폴더는 **복사-실행 가능한 dsync init 명령**을
보여주고, 등록 여부는 사용자가 결정한다(SETUP-2ND-PC.ps1 -Discover가 이 출력을 쓴다).

순회는 RemoteCollector를 쓰지 않는다 — 그쪽은 마커를 ALWAYS_EXCLUDE로 걸러 버린다.
raw 목록 API를 깊이 제한으로 돈다(비용은 폴더 수 비례 ~0.4초/폴더. 실제 루트들은
깊이 1~2라 기본 깊이 3이면 수십 폴더만 순회한다 — WORK 하위 2,392폴더 전체를 돌지
않는다).

실행: python tools\\discover_roots.py [--depth N] [--local-base 경로]
      --local-base 는 init 명령의 --local-root 를 채울 이 PC의 최상위 폴더
      (기본 C:\\Dooray — 새 PC 기준).
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dooray_sync.api.client import DoorayClient          # noqa: E402
from dooray_sync.api.drive import DriveAPI               # noqa: E402
from dooray_sync.auth import get_token                   # noqa: E402
from dooray_sync.config import config_path               # noqa: E402
from dooray_sync.util.paths import path_key, to_nfc      # noqa: E402

MARKER = "synchere.bat"
DEFAULT_DRIVE = "3229053305881780627"


def _registered() -> dict[str, str]:
    """{원격경로 path_key: 프로파일명} — config가 없으면(새 PC) 빈 dict."""
    cp = config_path()
    try:
        with open(cp, "rb") as f:
            doc = tomllib.load(f)
    except OSError:
        return {}
    out = {}
    for name, body in (doc.get("profile") or {}).items():
        rp = str((body or {}).get("remote_path") or "").strip()
        if rp:
            out[path_key(rp)] = name
    return out


def main() -> int:
    depth = 3
    local_base = r"C:\Dooray"
    args = sys.argv[1:]
    if "--depth" in args:
        i = args.index("--depth")
        depth = int(args[i + 1])
    if "--local-base" in args:
        i = args.index("--local-base")
        local_base = args[i + 1]

    registered = _registered()
    found: list[tuple[str, bool, str]] = []   # (원격경로, 등록여부, 프로파일명)

    with DoorayClient("https://api.gov-dooray.com", get_token()) as client:
        api = DriveAPI(client)
        root = api.find_root_folder(DEFAULT_DRIVE)
        # (folder_id, 경로, 깊이) BFS — 마커가 있는 폴더를 찾는다
        queue: list[tuple[str, str, int]] = [(root, "", 0)]
        scanned = 0
        while queue:
            fid, path, d = queue.pop(0)
            children = list(api.iter_children(DEFAULT_DRIVE, fid))
            scanned += 1
            has_marker = any(
                not c.is_dir and path_key(c.name) == path_key(MARKER) for c in children)
            if has_marker and path:
                key = path_key(path)
                found.append((path, key in registered, registered.get(key, "")))
            # 마커가 있는 폴더는 동기화 루트다 — 루트 안에 또 루트를 두지 않으므로
            # 그 하위트리는 내려가지 않는다. 이 가지치기가 없으면 spri 2025의 55개
            # 하위 폴더 같은 곳까지 전부 나열해 실측 281초가 걸렸다(가지치기 후 수십 초).
            if d < depth and not has_marker:
                for c in children:
                    if c.is_dir and c.sub_type != "trash":
                        sub = f"{path}/{to_nfc(c.name)}" if path else to_nfc(c.name)
                        queue.append((c.id, sub, d + 1))

    print(f"동기화 루트 마커 발견: {len(found)}건 (폴더 {scanned}개 순회, 깊이 ≤{depth})")
    print()
    for path, is_reg, prof in sorted(found):
        if is_reg:
            print(f"  [등록됨] {path}  (프로파일 '{prof}')")
    news = [(p, r, n) for p, r, n in sorted(found) if not r]
    if news:
        print()
        print("아직 이 PC에 등록되지 않은 동기화 루트 — 쓰려면 아래 명령으로 등록:")
        for path, _r, _n in news:
            leaf = path.rpartition("/")[2]
            print(f'  dsync init -p {leaf} --drive-id {DEFAULT_DRIVE} '
                  f'--remote-path "{path}" --local-root "{local_base}\\{leaf}"')
        print()
        print("  (로컬에 같은 파일이 이미 있다면 init 뒤 'dsync reconcile'을 먼저 실행)")
    elif not found:
        print("  마커가 없습니다. 주 PC에서 'python tools\\place_markers.py'로 배치하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
