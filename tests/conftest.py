"""테스트가 이 PC의 진짜 설정·상태에 닿지 못하게 막는다.

`_config_dir()`/`_state_root()`는 환경변수가 없으면 %APPDATA%·%LOCALAPPDATA%로
떨어진다(config.py). 그래서 **격리는 각 테스트가 기억해야 하는 예의**였고,
기억하지 못한 테스트는 조용히 실제 운영 데이터를 건드렸다.

실측 2026-08-18 (개발 PC HSY): `test_synchere.py`의 픽스처가 ENV_CONFIG_DIR만
덮고 ENV_STATE_DIR을 빠뜨려서, `sh.main()`을 부르는 테스트가 실행될 때마다
`%LOCALAPPDATA%\\dooray-sync\\auto\\notices.jsonl`을 **실제로 다시 썼다**
(`main()` → `_clear_auto_notices()` → `notify.clear()`는 지울 게 없어도 파일을
새로 쓴다). 자동 루프가 도는 PC에서 pytest를 한 번 돌리면 사람이 아직 보지
못한 '삭제 대기·충돌 대기' 통지가 사라진다. 하필 이 PC의 실제 프로파일 이름이
`folder`이고, 한글 폴더의 자동 생성 이름도 `folder`라 정확히 겹친다.

읽기 사고(AutoState({})가 진짜 state.json을 읽던 c760aa6)와 같은 계열이되
이쪽은 **쓰기**다. 그래서 그 자리만 고치지 않고 여기서 전역으로 막는다 —
앞으로 추가되는 테스트는 격리를 기억할 필요가 없고, 잊어도 새지 않는다.

개별 테스트가 자기 tmp_path로 다시 덮어쓰는 것은 그대로 동작한다(이 픽스처는
바닥값만 깔아 둔다).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dooray_sync import config as cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_user_dirs(tmp_path_factory, monkeypatch):
    """모든 테스트의 설정·상태 루트를 임시 폴더로 내린다(autouse).

    테스트마다 새 폴더를 준다 — 공유하면 한 테스트가 남긴 state.json이
    다음 테스트의 판정을 바꾸는, 지금 막으려는 바로 그 사고가 테스트끼리
    다시 생긴다.
    """
    base = tmp_path_factory.mktemp("userdirs")
    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(base / "cfg"))
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(base / "state"))
