"""프로파일 원격 루트 바로 아래의 **도구 파일**(synchere.bat 계열)을 휴지통으로 보낸다.

도구 파일은 이제 양축 ALWAYS_EXCLUDE라 동기화에서 보이지 않는다. 제외 도입 전에
업로드된 잔재를 정리할 때 쓴다. 안전 가드: 아래 허용 목록의 이름만 지울 수 있고,
프로파일 원격 루트의 직계 자식만 본다. 삭제는 전부 휴지통(복구 가능)이다.

실행: python tools\\trash_toolfile.py <프로파일> [<프로파일>...]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.api.client import DoorayClient                  # noqa: E402
from dooray_sync.api.drive import DriveAPI                       # noqa: E402
from dooray_sync.auth import get_token                           # noqa: E402
from dooray_sync.config import load_config                       # noqa: E402
from dooray_sync.core.remote import resolve_remote_root          # noqa: E402

ALLOWED_NAMES = ("동기화.bat", "synchere.bat")


def main() -> int:
    profiles = sys.argv[1:]
    if not profiles:
        print(__doc__)
        return 2
    rc = 0
    with DoorayClient("https://api.gov-dooray.com", get_token()) as client:
        api = DriveAPI(client)
        for prof in profiles:
            p = load_config(prof)
            root_id, prefix = resolve_remote_root(api, p.drive_id, p.remote_path)
            found = False
            for name in ALLOWED_NAMES:
                child = api.find_child_by_name(p.drive_id, root_id, name)
                if child is not None and not child.is_dir:
                    api.move_to_trash(p.drive_id, child.id)
                    print(f"[{prof}] 휴지통으로: {prefix or '/'}/{child.name} (id={child.id})")
                    found = True
            if not found:
                print(f"[{prof}] 도구 파일 없음 — 할 일 없음")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
