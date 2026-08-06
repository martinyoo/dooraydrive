"""특정 파일의 상태 DB 레코드를 읽기 전용으로 덤프한다 — 진단용.

실행: python tools\\probe_record.py <프로파일> <경로 부분문자열>
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.config import db_path   # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    prof, needle = sys.argv[1], sys.argv[2]
    p = str(db_path(prof))
    conn = sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM files WHERE rel_path LIKE ?", (f"%{needle}%",)).fetchall()
    if not rows:
        print("해당 레코드 없음")
        return 1
    for r in rows:
        print("-" * 70)
        for k in r.keys():
            v = r[k]
            if isinstance(v, str) and len(v) > 80:
                v = v[:77] + "..."
            print(f"  {k:<18}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
