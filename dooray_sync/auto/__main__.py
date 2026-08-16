"""`python -m dooray_sync.auto` — 시작프로그램 런처가 부르는 진입점.

런처(.bat)가 이 모듈을 직접 부르는 이유: synchere.bat을 경유하면 프로그램 폴더를
다시 탐색하게 되는데, 자동 경로는 등록 시점 절대경로만 써야 한다(I-A7).
사람이 치는 `synchere.bat --auto loop`와 **같은 run_loop()로 들어간다.**
"""
from __future__ import annotations

import sys

for _stream_name in ("stdout", "stderr"):
    try:
        getattr(sys, _stream_name).reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from .runner import run_loop  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_loop(extra=sys.argv[1:]))
