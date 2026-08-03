"""드라이브 최상위 항목 나열 — 읽기 전용.

시험 잔여물(`_m2_test`, `_ut_*` 등)이 원격에 남아 있는지 확인할 때 쓴다.
실행: python tools\\probe_root.py [drive_id]
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
    drive_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DRIVE
    with DoorayClient("https://api.gov-dooray.com", get_token()) as client:
        api = DriveAPI(client)
        root = api.find_root_folder(drive_id)
        print(f"drive_id={drive_id} root_id={root}")
        for child in api.iter_children(drive_id, root):
            kind = "dir " if child.is_dir else "file"
            print(f"  [{kind}] {child.name}  (subType={child.sub_type})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
