"""changes 커서 진단 — 읽기 전용. 아무것도 바꾸지 않는다.

원격을 고쳤는데 델타가 0건을 보고하는 상황의 원인을 판별한다.
실행: (환경변수가 설정된 그 PowerShell 창에서)
    python tools\\probe_changes.py m2test
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


def line(k, v):
    print(f"  {k:<28}: {v}")


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "m2test"
    p = load_config(profile)

    print("=" * 68)
    print(f"  changes 커서 진단 (profile={profile}) — 읽기 전용")
    print("=" * 68)
    line("drive_id", p.drive_id)
    line("remote_path", p.remote_path or "(드라이브 루트)")

    with Store(db_path(profile)) as store:
        stored = store.get_cursor()
        line("DB에 저장된 커서", f"revision={stored.revision} file_id={stored.file_id or '-'}")
        recs = {r.rel_path: r for r in store.iter_files(p.drive_id)}
        print()
        print("  [DB가 기억하는 원격 상태]")
        for rel, r in sorted(recs.items()):
            line(f"    {rel}", f"file_id={r.file_id} ver={r.remote_version} "
                                f"size={r.remote_size} md5={(r.remote_md5 or '-')[:12]} "
                                f"status={r.sync_status}")

    with DoorayClient(p.base_url, get_token()) as client:
        drive = DriveAPI(client)

        print()
        print("  [실제 원격 상태 — get_file_meta]")
        for rel, r in sorted(recs.items()):
            if not r.file_id:
                continue
            try:
                m = drive.get_file_meta(p.drive_id, r.file_id)
                same = (m.version == r.remote_version and m.size == r.remote_size)
                line(f"    {rel}", f"ver={m.version} size={m.size} "
                                   f"{'(DB와 동일)' if same else '<<< DB와 다름!'}")
            except Exception as exc:  # noqa: BLE001
                line(f"    {rel}", f"조회 실패: {type(exc).__name__}: {exc}")

        print()
        print("  [live tip 탐색]")
        tip = drive.advance_to_tip(p.drive_id, Cursor(revision=0))
        line("advance_to_tip(0)", f"revision={tip.revision}")

        print()
        print("  [저장된 커서 이후 변경]")
        items, nxt = drive.get_changes(p.drive_id, stored, size=50)
        line("건수", len(items))
        for it in items[:20]:
            line(f"    rev={it.revision}", f"{it.change_type} {it.file_type} "
                                           f"name={it.name} path={it.parent_path}")

        for back in (5, 20, 100):
            start = max(0, stored.revision - back)
            items2, _ = drive.get_changes(p.drive_id, Cursor(revision=start), size=50)
            print()
            line(f"  latestRevision={start} (커서-{back}) 건수", len(items2))
            for it in items2[:12]:
                line(f"    rev={it.revision}", f"{it.change_type} {it.file_type} "
                                               f"name={it.name} path={it.parent_path}")

    print()
    print("=" * 68)
    print("  위 출력을 그대로 복사해 알려 주세요.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
