"""원격 동기화 루트에 마커(synchere.bat)를 배치한다 — 발견(discovery)용.

정본 관계: config.toml = 실행의 정본(각 PC), 원격 마커 = **발견 힌트**(드라이브).
마커는 config에서 단방향 파생되며, sync 엔진은 마커를 보지 못한다(양축
ALWAYS_EXCLUDE — dooray_sync/core/scanner.py). 마커가 지워져도 동기화는 멈추지
않고, 마커를 손으로 만들어도 동기화가 시작되지 않는다. 마커의 의미는
"dooraydrive로 관리되는 루트"이며 방향·제외 정책은 각 PC의 config 소관이다.
따라서 push 전용(workenv)·미전환(study/writing) 프로파일에도 마커를 둔다.

실행: python tools\\place_markers.py            # config의 전 프로파일
      python tools\\place_markers.py 프로파일 [프로파일...]
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dooray_sync.api.client import DoorayClient                  # noqa: E402
from dooray_sync.api.drive import DriveAPI                       # noqa: E402
from dooray_sync.auth import get_token                           # noqa: E402
from dooray_sync.config import config_path, load_config          # noqa: E402
from dooray_sync.core.remote import resolve_remote_root          # noqa: E402

MARKER = "synchere.bat"


def _all_profiles() -> list[str]:
    with open(config_path(), "rb") as f:
        return list((tomllib.load(f).get("profile") or {}).keys())


def main() -> int:
    profiles = sys.argv[1:] or _all_profiles()
    src = REPO / MARKER
    if not src.exists():
        print(f"마커 원본이 없습니다: {src}")
        return 2
    rc = 0
    with DoorayClient("https://api.gov-dooray.com", get_token()) as client:
        api = DriveAPI(client)
        for prof in profiles:
            try:
                p = load_config(prof)
                root_id, prefix = resolve_remote_root(api, p.drive_id, p.remote_path)
                child = api.find_child_by_name(p.drive_id, root_id, MARKER)
                if child is not None:
                    print(f"[{prof}] 이미 있음 — {prefix or '/'}/{MARKER}")
                    continue
                api.upload_new(p.drive_id, root_id, MARKER, src)
                print(f"[{prof}] 배치함 — {prefix or '/'}/{MARKER}")
            except Exception as exc:  # noqa: BLE001 — 프로파일 단위 격리
                print(f"[{prof}] 실패: {type(exc).__name__}: {exc}")
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
