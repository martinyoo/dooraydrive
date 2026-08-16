"""무인 삭제 등급화(--max-auto-deletes/--max-auto-delete-mb)와 gone 탐침 분리.

계획서 단위 4의 게이트 기준을 고정한다: 임계 초과 삭제는 **삭제만** 보류되고
나머지 동작은 남으며, 보류 건수가 0이 아닌 실제 숫자로 보인다.
"""
from __future__ import annotations

import datetime as dt

from dooray_sync.cli.main import (
    GONE_PROBE_BUDGET,
    _apply_delete_grade,
    _gone_probe_ids,
)
from dooray_sync.core.differ import KIND_REMOTE_TRASH, KIND_UPLOAD_NEW
from dooray_sync.core.planner import Action, Plan
from dooray_sync.store.db import FileRecord


def _rec(key: str, *, file_id: str = "", size: int = 0, is_dir: bool = False,
         seen: str = "", status: str = "synced") -> FileRecord:
    return FileRecord(
        drive_id="d", rel_path=key, file_id=file_id or None, is_dir=is_dir,
        local_size=size or None, sync_status=status,
        last_synced_at=seen or None)


def _plan_with_deletes(n: int, *, extra_upload: bool = True) -> tuple[Plan, dict]:
    pl = Plan()
    base: dict[str, FileRecord] = {}
    for i in range(n):
        key = f"del{i}.txt"
        pl.actions.append(Action(kind=KIND_REMOTE_TRASH, rel_path=key, key=key))
        base[key] = _rec(key, file_id=f"f{i}", size=1024)
    if extra_upload:
        pl.actions.append(Action(kind=KIND_UPLOAD_NEW, rel_path="up.txt", key="up.txt"))
        pl.counts[KIND_UPLOAD_NEW] = 1
    pl.counts[KIND_REMOTE_TRASH] = n
    pl.delete_count = n
    pl.delete_actions = n
    # 등급화의 1% 기준이 걸리지 않도록 기준선을 충분히 크게
    for i in range(1000):
        base.setdefault(f"keep{i}.txt", _rec(f"keep{i}.txt", file_id=f"k{i}", size=1))
    return pl, base


def test_over_count_defers_deletes_only():
    """6건 > 임계 5건 → 삭제 전량 보류, 업로드는 남는다(계획서 게이트: 6건 보류)."""
    pl, base = _plan_with_deletes(6)
    deferred, why = _apply_delete_grade(pl, base, max_deletes=5, max_mb=50,
                                        base_count=len(base))
    assert deferred == 6                      # 0이 아닌 실제 숫자
    assert "건수" in why
    assert all(a.kind != KIND_REMOTE_TRASH for a in pl.actions)
    assert any(a.kind == KIND_UPLOAD_NEW for a in pl.actions)   # 삭제만 떼어냈다
    assert pl.delete_count == 0
    assert len(pl.deferred) == 6


def test_under_all_thresholds_allows():
    """4건 <= 5건, 4KB <= 50MB, 1% 이하 → 그대로 실행(계획서 게이트: 4건 허용)."""
    pl, base = _plan_with_deletes(4)
    deferred, why = _apply_delete_grade(pl, base, max_deletes=5, max_mb=50,
                                        base_count=len(base))
    assert (deferred, why) == (0, "")
    assert pl.delete_count == 4
    assert sum(1 for a in pl.actions if a.kind == KIND_REMOTE_TRASH) == 4


def test_over_bytes_defers():
    pl, base = _plan_with_deletes(2)
    base["del0.txt"].local_size = 60 * 1024 * 1024   # 60MB > 50MB
    deferred, why = _apply_delete_grade(pl, base, max_deletes=5, max_mb=50,
                                        base_count=len(base))
    assert deferred == 2
    assert "용량" in why


def test_over_baseline_ratio_defers():
    """건수·용량은 통과해도 기준선 1%를 넘으면 보류 — 작은 폴더 전멸 방어."""
    pl, base = _plan_with_deletes(3)
    deferred, why = _apply_delete_grade(pl, base, max_deletes=5, max_mb=50,
                                        base_count=100)   # 1% = 1건 < 3건
    assert deferred == 3
    assert "1%" in why


def test_folder_delete_counts_subtree_bytes():
    pl = Plan()
    pl.actions = [Action(kind=KIND_REMOTE_TRASH, rel_path="folder", key="folder",
                         is_dir=True)]
    pl.counts = {KIND_REMOTE_TRASH: 1}
    pl.delete_count = 3
    pl.delete_actions = 1
    base = {
        "folder": _rec("folder", file_id="f0", is_dir=True),
        "folder/a.bin": _rec("folder/a.bin", file_id="f1", size=30 * 1024 * 1024),
        "folder/b.bin": _rec("folder/b.bin", file_id="f2", size=25 * 1024 * 1024),
    }
    deferred, why = _apply_delete_grade(pl, base, max_deletes=5, max_mb=50,
                                        base_count=10_000)
    assert deferred == 3          # 하위 합산 55MB > 50MB
    assert "용량" in why


def test_gone_probe_delete_on_probes_all():
    """전파 켠 실행은 전량 확인(예전 동작) — 삭제가 base를 정리하므로 반복 비용 없음."""
    entries: dict = {}
    base = {f"g{i}": _rec(f"g{i}", file_id=f"f{i}") for i in range(GONE_PROBE_BUDGET + 50)}
    got = _gone_probe_ids(base, entries, do_delete=True)
    assert len(got) == GONE_PROBE_BUDGET + 50


def test_gone_probe_no_delete_caps_and_rotates():
    """전파 끈 실행: 상한 + 최근 확인분 건너뛰기 + 오래된 것 우선."""
    now = dt.datetime.now()
    old = (now - dt.timedelta(hours=48)).isoformat(timespec="seconds")
    older = (now - dt.timedelta(hours=72)).isoformat(timespec="seconds")
    recent = now.isoformat(timespec="seconds")
    base = {
        "a": _rec("a", file_id="fa", seen=older),
        "b": _rec("b", file_id="fb", seen=old),
        "c": _rec("c", file_id="fc", seen=recent),      # 24h 이내 — 건너뜀
        "d": _rec("d", file_id="fd", status="unsyncable", seen=older),  # 제외
    }
    got = _gone_probe_ids(base, {}, do_delete=False)
    assert got == ["fa", "fb"]                # 오래된 순, recent·unsyncable 제외
    many = {f"m{i}": _rec(f"m{i}", file_id=f"f{i:04d}", seen=older)
            for i in range(GONE_PROBE_BUDGET + 30)}
    assert len(_gone_probe_ids(many, {}, do_delete=False)) == GONE_PROBE_BUDGET


def test_gone_probe_skips_present_entries():
    base = {"here": _rec("here", file_id="fh")}
    assert _gone_probe_ids(base, {"here": object()}, do_delete=True) == []
