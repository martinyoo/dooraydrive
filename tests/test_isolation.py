"""테스트 격리 자체를 검사한다 — conftest.py의 autouse 가드가 살아 있는가.

이 파일이 지키는 것은 기능이 아니라 **테스트 하네스의 성질**이다. 격리가
풀리면 다른 테스트들은 여전히 초록으로 통과하면서 이 PC의 실제 운영 데이터를
건드린다(2026-08-18 실측: pytest 한 번이 자동 루프의 notices.jsonl을 덮어썼다).
조용히 통과하는 사고라서, 겨냥해 두지 않으면 아무도 못 본다.
"""
from __future__ import annotations

import os
from pathlib import Path

from dooray_sync import config as cfg
from dooray_sync.auto import notify, state


def _real_roots() -> list[Path]:
    """이 PC의 진짜 사용자 폴더들. 테스트 산출물이 여기 안으로 들어가면 안 된다."""
    out = []
    for var in ("APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(var, "").strip()
        if value:
            out.append(Path(value) / cfg.APP_NAME)
    return out


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def test_env_overrides_are_set_for_every_test():
    """autouse 픽스처가 두 변수를 **모두** 깔아 둔다 — 하나만 덮는 것이 사고였다."""
    assert os.environ.get(cfg.ENV_CONFIG_DIR, "").strip()
    assert os.environ.get(cfg.ENV_STATE_DIR, "").strip()


def test_config_and_state_paths_are_outside_real_user_dirs():
    paths = [cfg.config_path(), state.state_path(), notify.notices_path(),
             cfg.state_dir("WORK")]
    for real in _real_roots():
        for p in paths:
            assert not _under(p, real), f"{p} 가 실제 사용자 폴더 {real} 안에 있다"


def test_writing_a_notice_does_not_reach_the_real_state_dir(tmp_path):
    """실제로 써 본다 — 경로 계산만 맞고 쓰기가 다른 곳으로 가는 경우를 막는다."""
    notify.add("folder", "held", "격리 검사")
    written = notify.notices_path()
    assert written.exists()
    for real in _real_roots():
        assert not _under(written, real)
