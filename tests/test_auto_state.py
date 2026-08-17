r"""AutoState의 '빈 상태로 시작' vs '디스크에서 읽기' 구분.

2026-08-18 실측 사고: `__init__`이 `if data else load_state()`(진리값 판정)를
썼다. 빈 dict `{}`는 falsy라 "명시적으로 빈 상태" 요청이 "인자 생략 = 디스크
읽기"와 구분되지 않았다. tests/test_auto_decide.py의 `_st()`가 인자 없이
`AutoState({})`를 만들 때마다 **이 개발 PC에서 실제로 도는 루프의 진짜
state.json**을 몰래 읽었고, 날짜가 지나며 실제 값이 쌓이자 결정론적이어야 할
순수 판정 테스트가 기기 상태에 따라 흔들렸다. 프로덕션 코드는 전부
`AutoState()`(인자 없음)로만 불러 실행에는 영향이 없었다 — 테스트 격리만
깨져 있었다.
"""
from __future__ import annotations

import pytest

from dooray_sync import config as cfg
from dooray_sync.auto.state import AutoState, save_state


@pytest.fixture()
def statedir(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(tmp_path / "state"))
    return tmp_path


def test_explicit_empty_dict_does_not_touch_disk(statedir):
    """디스크에 진짜 상태가 있어도 {}를 명시하면 빈 상태로 시작해야 한다."""
    save_state({"last_tick": "2099-01-01T00:00:00", "day_start": "2099-01-01T00:00:00"})

    st = AutoState({})

    assert st.last_tick == ""
    assert st.day_start == ""


def test_no_argument_loads_from_disk(statedir):
    """인자를 아예 안 주면(None) 디스크의 실제 상태를 읽는다 — 프로덕션 경로."""
    save_state({"last_tick": "2026-08-18T09:00:00"})

    st = AutoState()

    assert st.last_tick == "2026-08-18T09:00:00"


def test_nonempty_dict_is_used_verbatim(statedir):
    """비어 있지 않은 dict는 원래도 정상 동작했다 — 회귀 확인용."""
    save_state({"last_tick": "2099-01-01T00:00:00"})

    st = AutoState({"last_tick": "2026-08-18T08:00:00"})

    assert st.last_tick == "2026-08-18T08:00:00"
