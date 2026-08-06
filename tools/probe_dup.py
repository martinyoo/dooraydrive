"""부분문자열로 일치하는 레코드들의 경로를 코드포인트 수준으로 대조한다 — 진단용.

실행: python tools\\probe_dup.py <프로파일> <경로 부분문자열>
"""
from __future__ import annotations

import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.config import db_path   # noqa: E402


def main() -> int:
    prof, needle = sys.argv[1], sys.argv[2]
    p = str(db_path(prof))
    conn = sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT rel_path, rel_path_key, file_id, local_size, sync_status "
        "FROM files WHERE rel_path LIKE ?", (f"%{needle}%",)).fetchall()
    print(f"{len(rows)}건 일치")
    paths = []
    for r in rows:
        paths.append(r["rel_path"])
        print("-" * 70)
        print(f"  rel_path : {r['rel_path']}")
        print(f"  key      : {r['rel_path_key']}")
        print(f"  file_id  : {r['file_id']}  size={r['local_size']}  status={r['sync_status']}")
        norm = unicodedata.normalize("NFC", r["rel_path"])
        print(f"  NFC 동일 : {norm == r['rel_path']}  길이={len(r['rel_path'])}")
    if len(paths) == 2:
        a, b = paths
        print("=" * 70)
        print("두 경로의 차이 (첫 불일치 지점):")
        for i, (ca, cb) in enumerate(zip(a, b)):
            if ca != cb:
                print(f"  위치 {i}: {ca!r}(U+{ord(ca):04X}) vs {cb!r}(U+{ord(cb):04X})")
                print(f"  문맥 A: ...{a[max(0,i-25):i+8]}...")
                print(f"  문맥 B: ...{b[max(0,i-25):i+8]}...")
                break
        else:
            print(f"  공통 접두 동일, 길이 차이: {len(a)} vs {len(b)}")
            longer = a if len(a) > len(b) else b
            print(f"  긴 쪽 꼬리: ...{longer[min(len(a), len(b)) - 10:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
