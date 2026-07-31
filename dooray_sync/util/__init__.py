"""공통 유틸 (경로/해시/잠금).

서브모듈을 여기서 import하지 않는다 — lock 모듈은 비Windows에서 의도적으로
import 실패하므로, 그 실패가 paths/hashing 사용까지 끌고 가면 안 된다.
"""
from __future__ import annotations
