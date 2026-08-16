"""--unattended 강등 모드 — 설계 불변식 I-A2(무인은 로컬 원본을 개명하지 않는다)와
I-A5(로컬 붕괴 시 아무것도 하지 않는다)를 고정한다.
"""
from __future__ import annotations

from dooray_sync.cli.main import _demote_conflicts
from dooray_sync.core.differ import (
    KIND_CONFLICT,
    KIND_DOWNLOAD_UPDATE,
    Decision,
)
from dooray_sync.core.planner import Action, Plan


def _conflict_action(key: str, size: int) -> Action:
    d = Decision(case=6, kind=KIND_CONFLICT, rel_path=key, key=key)
    # Decision.size는 remote/local에서 유도되는 property일 수 있어 직접 못 넣는다 —
    # planner가 bytes_down에 더한 값과 같은 크기를 강등이 빼는지만 본다.
    return Action(kind=KIND_CONFLICT, rel_path=key, key=key, decision=d)


def test_demote_moves_conflicts_to_deferred():
    pl = Plan()
    pl.actions = [
        _conflict_action("a.docx", 100),
        Action(kind=KIND_DOWNLOAD_UPDATE, rel_path="b.txt", key="b.txt"),
        _conflict_action("c.hwp", 200),
    ]
    pl.counts = {KIND_CONFLICT: 2, KIND_DOWNLOAD_UPDATE: 1}

    n = _demote_conflicts(pl)

    assert n == 2
    assert [a.kind for a in pl.actions] == [KIND_DOWNLOAD_UPDATE]   # 나머지는 진행
    assert KIND_CONFLICT not in pl.counts
    assert len(pl.deferred) == 2
    assert all("사람 실행" in why for _rel, why in pl.deferred)


def test_demote_noop_without_conflicts():
    pl = Plan()
    pl.actions = [Action(kind=KIND_DOWNLOAD_UPDATE, rel_path="b.txt", key="b.txt")]
    pl.counts = {KIND_DOWNLOAD_UPDATE: 1}
    assert _demote_conflicts(pl) == 0
    assert len(pl.actions) == 1
    assert pl.deferred == []
