"""Dooray Drive API 계층.

- `models`: 응답 dict → 값 객체 (RemoteFile / ChangeItem / Cursor)
- `client`: HTTP 단일 관문 (envelope 검사, 307 수동 추적, rate-limit 감속)
- `drive`: Drive 엔드포인트 래퍼 (영구삭제 메서드는 의도적 미구현)

이 패키지 __init__은 의도적으로 아무것도 재수출하지 않는다 —
병렬 구현 중 모듈 간 import 순서에 결합이 생기는 것을 피하기 위함.
사용측은 `from dooray_sync.api.models import RemoteFile`처럼 모듈을 직접 지정한다.
"""
from __future__ import annotations
