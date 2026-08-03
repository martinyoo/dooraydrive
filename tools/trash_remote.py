"""드라이브 최상위의 시험용 폴더를 휴지통으로 보낸다 (복구 가능).

안전 가드: 이름이 '_'로 시작하는 폴더만 받는다 — 시험 폴더(_usertest, _m2_test,
_poc_sandbox) 전용이며 실업무 폴더(WORK 등)는 이 도구로 지울 수 없다.

실행: python tools\\trash_remote.py _usertest [drive_id]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.api.client import DoorayClient          # noqa: E402
from dooray_sync.api.drive import DriveAPI               # noqa: E402
from dooray_sync.auth import get_token                   # noqa: E402

DEFAULT_DRIVE = "3229053305881780627"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    if not name.startswith("_"):
        print(f"거부: '{name}' — '_'로 시작하는 시험 폴더만 지울 수 있습니다.")
        return 2
    drive_id = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DRIVE

    with DoorayClient("https://api.gov-dooray.com", get_token()) as client:
        api = DriveAPI(client)
        root = api.find_root_folder(drive_id)
        child = api.find_child_by_name(drive_id, root, name)
        if child is None:
            print(f"최상위에 '{name}' 폴더가 없습니다 — 할 일 없음.")
            return 0
        api.move_to_trash(drive_id, child.id)
        print(f"휴지통으로 보냈습니다: {name} (id={child.id}) — 웹 휴지통에서 복구 가능")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
