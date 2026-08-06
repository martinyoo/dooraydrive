"""CLI와 같은 로더(all_by_key)로 레코드를 읽어 _has_baseline 판정을 재현한다 — 진단용.

실행: python tools\\probe_baseline.py <프로파일> <경로 부분문자열>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.config import db_path, load_config      # noqa: E402
from dooray_sync.core.differ import _has_baseline        # noqa: E402
from dooray_sync.store.db import Store                   # noqa: E402


def main() -> int:
    prof, needle = sys.argv[1], sys.argv[2]
    p = load_config(prof)
    with Store(db_path(prof)) as store:
        base = store.all_by_key(p.drive_id)
        hits = [(k, r) for k, r in base.items() if needle in r.rel_path]
        print(f"base 레코드 총 {len(base)}건 / '{needle}' 일치 {len(hits)}건")
        for k, r in hits:
            print(f"  key={k[:60]}...")
            print(f"  local_md5={r.local_md5!r} is_dir={r.is_dir!r} "
                  f"sync_status={r.sync_status!r}")
            print(f"  remote_version={r.remote_version!r} remote_revision={r.remote_revision!r}")
            print(f"  _has_baseline={_has_baseline(r)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
