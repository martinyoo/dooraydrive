"""동기화 코어 — 로컬/원격 스냅샷, 3-way diff, 계획·실행.

api 패키지와 같은 이유로 여기서 서브모듈을 재수출하지 않는다(병렬 구현 중
모듈 간 import 순서에 결합이 생기는 것을 피하기 위함).
사용측은 `from dooray_sync.core.scanner import LocalScanner`처럼 모듈을 직접 지정한다.
"""
from __future__ import annotations
