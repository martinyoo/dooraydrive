"""CLI 패키지.

typer 앱 본체는 `dooray_sync.cli.main:app`이다. api/core 패키지와 같은 이유로
여기서 재수출하지 않는다 — import 부수효과(로깅 구성·typer 초기화)가 단순히
패키지를 참조하는 것만으로 일어나면 곤란하다.

실행:
    python -m dooray_sync.cli.main <command>
    dsync <command>          # 콘솔 스크립트로 설치한 경우
"""
from __future__ import annotations
