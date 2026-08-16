"""dooray_sync — Dooray Drive 로컬 동기화 CLI.

설계·규약은 docs/모듈규약.md, 실측 근거는 docs/검토보고서.md 를 따른다.
"""
from __future__ import annotations

# M3(자동 동기화)부터 버전이 실제로 표시된다 — status·로그 첫 줄·--auto status.
# 릴리스마다 올릴 것: 이 값이 멈춰 있으면 동료 PC에 무엇이 깔렸는지 알 수 없다
# (배포 전략 항목 A — 갱신 모드(fca71ed)와 롤백 판단의 전제).
__version__ = "0.2.0"
