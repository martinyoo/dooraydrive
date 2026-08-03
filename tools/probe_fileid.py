"""changes 커서의 fileId 파라미터 대조 실험 — 읽기 전용.

가설: `fileId`는 페이징 보조키가 아니라 **그 파일로 결과를 거르는 필터**다.
그렇다면 커서에 fileId가 박히는 순간 이후 모든 변경이 영구 누락된다(R11 계열).

변수 하나만 바꿔 확인한다: **같은 latestRevision**에 fileId 유무만 다르게 질의한다.
실행: python tools\\probe_fileid.py m2test
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.api.client import DoorayClient          # noqa: E402
from dooray_sync.api.drive import DriveAPI               # noqa: E402
from dooray_sync.api.models import Cursor                # noqa: E402
from dooray_sync.auth import get_token                   # noqa: E402
from dooray_sync.config import db_path, load_config      # noqa: E402
from dooray_sync.store.db import Store                   # noqa: E402


def show(drive, drive_id, label, cur):
    items, _ = drive.get_changes(drive_id, cur, size=50)
    print(f"\n  [{label}]")
    print(f"    질의 파라미터: {cur.as_params()}")
    print(f"    반환 건수    : {len(items)}")
    for it in items[:10]:
        print(f"      rev={it.revision:<8} {it.change_type:<8} {it.file_type:<7} "
              f"name={it.name} id={it.file_id}")
    return items


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "m2test"
    p = load_config(profile)
    with Store(db_path(profile)) as store:
        stored = store.get_cursor()

    print("=" * 70)
    print("  changes fileId 파라미터 대조 실험 — 읽기 전용")
    print("=" * 70)
    print(f"  저장된 커서: revision={stored.revision} file_id={stored.file_id}")

    rev = stored.revision
    with DoorayClient(p.base_url, get_token()) as client:
        drive = DriveAPI(client)

        a = show(drive, p.drive_id, f"A. latestRevision={rev} + fileId={stored.file_id}",
                 Cursor(revision=rev, file_id=stored.file_id))
        b = show(drive, p.drive_id, f"B. latestRevision={rev} (fileId 없음)",
                 Cursor(revision=rev, file_id=None))

        # 다른 파일의 id를 fileId로 넣으면? (필터라면 그 파일 것만 나와야 한다)
        other = None
        if b:
            other = b[0].file_id
            show(drive, p.drive_id, f"C. latestRevision={rev-5} + fileId={other} (B의 첫 항목 id)",
                 Cursor(revision=rev - 5, file_id=other))
            show(drive, p.drive_id, f"D. latestRevision={rev-5} (fileId 없음)",
                 Cursor(revision=rev - 5, file_id=None))

    print()
    print("=" * 70)
    print("  판정")
    print(f"    A(fileId 있음) {len(a)}건  vs  B(fileId 없음) {len(b)}건")
    if len(b) > len(a):
        print("    → fileId가 결과를 거른다. 커서에 fileId를 넣으면 변경을 영구 누락한다.")
    elif len(a) == len(b):
        print("    → fileId는 결과에 영향이 없다. 원인은 다른 곳이다.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
