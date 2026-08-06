"""M2 회귀 테스트 — 양방향·충돌·저널 복구.

각 테스트는 "이게 깨지면 사용자 데이터가 사라진다"에 대응한다.
실행: python -m pytest tests -q   (pytest 미설치 시 python tests/test_m2.py)
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import os
import sys
from pathlib import Path

import typer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.api.client import DoorayApiError  # noqa: E402
from dooray_sync.api.drive import NO_ACCESS_AUTHORITY, DriveAPI  # noqa: E402
from dooray_sync.api.models import Cursor, RemoteFile  # noqa: E402
from dooray_sync.config import Profile  # noqa: E402
from dooray_sync.core.differ import (  # noqa: E402
    KIND_CONFLICT,
    KIND_DOWNLOAD_NEW,
    KIND_DOWNLOAD_UPDATE,
    KIND_FORGET,
    KIND_LOCAL_MOVE,
    KIND_LOCAL_TRASH,
    KIND_MKDIR_LOCAL,
    KIND_REMOTE_MOVE,
    KIND_REMOTE_TRASH,
    KIND_REPORT,
    KIND_TOUCH_BASE,
    KIND_UPLOAD_NEW,
    KIND_UPLOAD_VERSION,
    conflict_copy_name,
    diff,
)
from dooray_sync.core.executor import SyncExecutor  # noqa: E402
from dooray_sync.core.journal import SyncJournal, recover  # noqa: E402
from dooray_sync.core.planner import BulkDeleteAbort  # noqa: E402
from dooray_sync.core.planner import plan as build_plan  # noqa: E402
from dooray_sync.core.remote import (  # noqa: E402
    RemoteCollector,
    RemoteEntry,
    RemoteRootError,
    RemoteView,
    resolve_remote_root_anchored,
)
from dooray_sync.core.scanner import LocalEntry, LocalScanner  # noqa: E402
from dooray_sync.store.db import FileRecord, Store  # noqa: E402
from dooray_sync.util.paths import ext_path, path_key  # noqa: E402


# =========================================================== 도우미
def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def L(rel: str, md5: str = "", *, size: int = 10, mtime: int = 111,
      is_dir: bool = False, disk: str = "") -> LocalEntry:
    return LocalEntry(rel_path=rel, rel_path_key=path_key(rel), is_dir=is_dir,
                      disk_path=disk, mtime_ns=mtime, size=size, md5=md5 or None)


def R(rel: str, md5: str | None = None, *, ver: int = 1, size: int = 10,
      is_dir: bool = False, fid: str = "F1") -> RemoteEntry:
    return RemoteEntry(rel_path=rel, rel_path_key=path_key(rel), is_dir=is_dir, file_id=fid,
                       server_name=rel.rpartition("/")[2], version=ver, size=size, md5=md5)


def B(rel: str, *, md5: str = "aa", ver: int = 1, size: int = 10, mtime: int = 111,
      fid: str = "F1", is_dir: bool = False, status: str = "synced") -> FileRecord:
    return FileRecord(drive_id="d", rel_path=rel, file_id=fid, is_dir=is_dir,
                      local_mtime_ns=mtime, local_size=size, local_md5=md5,
                      remote_version=ver, remote_md5=md5, remote_size=size,
                      server_name=rel.rpartition("/")[2], sync_status=status)


def kinds(decisions) -> list[tuple[int, str]]:
    return [(d.case, d.kind) for d in decisions]


# =========================================================== 결정표 13케이스
def test_case_1_2_3_no_baseline():
    k = path_key("a.txt")
    # 1: 로컬에만 있음 → 올린다
    d, _ = diff(base={}, local={k: L("a.txt", "aa")}, remote=RemoteView(is_complete=True))
    assert kinds(d) == [(1, KIND_UPLOAD_NEW)]
    # 2: 원격에만 있음 → 받는다
    d, _ = diff(base={}, local={},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "aa")}))
    assert kinds(d) == [(2, KIND_DOWNLOAD_NEW)]
    # 3: 양쪽 신규 · 내용 동일 → 전송 없이 기록만
    d, _ = diff(base={}, local={k: L("a.txt", "aa")},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "aa")}))
    assert kinds(d) == [(3, KIND_TOUCH_BASE)]
    # 3: 양쪽 신규 · 내용 상이 → 충돌(양쪽 보존)
    d, _ = diff(base={}, local={k: L("a.txt", "aa")},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "bb")}))
    assert kinds(d) == [(3, KIND_CONFLICT)]


def test_case_4_5_6_modifications():
    k = path_key("a.txt")
    base = {k: B("a.txt")}
    # 4: 로컬만 수정
    d, _ = diff(base=base, local={k: L("a.txt", "cc", mtime=222)},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "aa")}))
    assert kinds(d) == [(4, KIND_UPLOAD_VERSION)]
    # 5: 원격만 수정
    d, _ = diff(base=base, local={k: L("a.txt", "aa")},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "bb", ver=2)}))
    assert kinds(d) == [(5, KIND_DOWNLOAD_UPDATE)]
    # 6: 양쪽 수정 → 충돌
    d, _ = diff(base=base, local={k: L("a.txt", "cc", mtime=222)},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "bb", ver=2)}))
    assert kinds(d) == [(6, KIND_CONFLICT)]
    # 6: 양쪽이 바뀌었지만 내용이 같으면 가짜 충돌 — 충돌로 만들지 않는다
    d, _ = diff(base=base, local={k: L("a.txt", "zz", mtime=222)},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "zz", ver=2)}))
    assert kinds(d) == [(6, KIND_TOUCH_BASE)]


def test_case_7_8_deletes_respect_propagate_flag():
    k = path_key("a.txt")
    base = {k: B("a.txt")}
    remote_same = RemoteView(is_complete=True, entries={k: R("a.txt", "aa")})
    remote_gone = RemoteView(is_complete=True, deleted_keys={k})

    # 기본값(전파 꺼짐)은 어느 방향이든 '보고만'
    assert kinds(diff(base=base, local={}, remote=remote_same)[0]) == [(7, KIND_REPORT)]
    assert kinds(diff(base=base, local={k: L("a.txt", "aa")},
                      remote=remote_gone)[0]) == [(8, KIND_REPORT)]
    # 켜면 반대쪽 휴지통으로
    assert kinds(diff(base=base, local={}, remote=remote_same,
                      propagate_deletes=True)[0]) == [(7, KIND_REMOTE_TRASH)]
    assert kinds(diff(base=base, local={k: L("a.txt", "aa")}, remote=remote_gone,
                      propagate_deletes=True)[0]) == [(8, KIND_LOCAL_TRASH)]


def test_case_9_10_preservation_always_wins():
    """삭제 vs 수정에서 **삭제는 진다**. 전파가 켜져 있어도 마찬가지다(규약_M2 I2)."""
    k = path_key("a.txt")
    base = {k: B("a.txt")}

    # 9: 로컬 삭제 · 원격 수정 → 원격본을 되받는다(로컬 삭제를 전파하지 않는다)
    d, _ = diff(base=base, local={},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "bb", ver=2)}),
                propagate_deletes=True)
    assert kinds(d) == [(9, KIND_DOWNLOAD_NEW)]

    # 10: 로컬 수정 · 원격 삭제 → 다시 올린다(로컬을 지우지 않는다)
    d, _ = diff(base=base, local={k: L("a.txt", "cc", mtime=222)},
                remote=RemoteView(is_complete=True, deleted_keys={k}),
                propagate_deletes=True)
    assert kinds(d) == [(10, KIND_UPLOAD_NEW)]


def test_case_11_12_moves():
    old, new = path_key("a.txt"), path_key("sub/a.txt")
    base = {old: B("a.txt")}
    # 12: 원격에서 이동 → 로컬도 옮긴다(다시 받지 않는다)
    d, _ = diff(base=base, local={old: L("a.txt", "aa")},
                remote=RemoteView(is_complete=False, entries={new: R("sub/a.txt", "aa")}))
    assert kinds(d) == [(12, KIND_LOCAL_MOVE)]
    assert d[0].new_rel_path == "sub/a.txt"
    # 11: 로컬에서 이동 → 원격도 옮긴다(재업로드하지 않는다)
    d, _ = diff(base=base, local={new: L("sub/a.txt", "aa")},
                remote=RemoteView(is_complete=False))
    assert kinds(d) == [(11, KIND_REMOTE_MOVE)]


def test_local_move_not_promoted_when_ambiguous():
    """같은 크기·내용 후보가 여럿이면 이동으로 승격하지 않는다 — 오판이 곧 오이동이다."""
    base = {path_key("a.txt"): B("a.txt"), path_key("b.txt"): B("b.txt", fid="F2")}
    local = {path_key("x.txt"): L("x.txt", "aa"), path_key("y.txt"): L("y.txt", "aa")}
    d, _ = diff(base=base, local=local, remote=RemoteView(is_complete=False))
    assert not any(x.kind == KIND_REMOTE_MOVE for x in d)


def test_case_13_move_plus_modify_protects_local():
    """원격은 이동+내용 수정, 로컬도 수정(3중 발산) — 옮기고 덮으면 편집이 사라진다.
    둘 다 남긴다."""
    old, new = path_key("a.txt"), path_key("sub/a.txt")
    d, _ = diff(base={old: B("a.txt")}, local={old: L("a.txt", "cc", mtime=222)},
                remote=RemoteView(is_complete=False, entries={new: R("sub/a.txt", "bb", ver=2)}))
    got = {x.kind for x in d}
    assert "PROTECT" in got and KIND_DOWNLOAD_NEW in got
    assert KIND_LOCAL_MOVE not in got


def test_case_13_pure_move_carries_local_edit_along():
    """UT-12(2026-08-04): 원격이 **자리만** 옮기고(내용 불변) 로컬이 수정된 경우 —
    보호로 세워 두지 않고 편집본을 새 경로로 함께 옮긴다. base는 옛 기준선을
    유지하므로 다음 패스 결정표 4가 편집을 업로드한다."""
    old, new = path_key("a.txt"), path_key("sub/a.txt")
    d, _ = diff(base={old: B("a.txt")}, local={old: L("a.txt", "cc", mtime=222)},
                remote=RemoteView(is_complete=False,
                                  entries={new: R("sub/a.txt", "aa", ver=1)}))
    got = {x.kind for x in d}
    assert KIND_LOCAL_MOVE in got, got
    assert "PROTECT" not in got and KIND_DOWNLOAD_NEW not in got


def test_remote_move_already_applied_is_not_reprocessed():
    """UT-12(2026-08-04): C13 보호 후 옛 레코드가 남은 상태(새 키에 같은 file_id의
    레코드가 이미 있음)를 다시 '이동'으로 처리하면 새 경로를 매 패스 재수신하는
    무한 반복이 된다. 이동은 실현된 것으로 보고, 옛 키의 로컬 편집본은 결정표
    10(보존 승리)으로 올린다."""
    old, new = path_key("a.txt"), path_key("sub/a.txt")
    base = {old: B("a.txt"), new: B("sub/a.txt", md5="bb", ver=2)}
    local = {old: L("a.txt", "cc", mtime=222), new: L("sub/a.txt", "bb", mtime=333)}
    # 완전 뷰: 원격에는 새 경로만 있다
    d, _ = diff(base=base, local=local,
                remote=RemoteView(is_complete=True,
                                  entries={new: R("sub/a.txt", "bb", ver=2)}))
    by_key = {}
    for x in d:
        by_key.setdefault(x.key, []).append(x.kind)
    # 새 경로: 이미 동기화됨 — 재수신 없음
    assert KIND_DOWNLOAD_NEW not in by_key.get(new, []), by_key
    assert KIND_LOCAL_MOVE not in by_key.get(old, []), by_key
    # 옛 경로의 편집본: 보존 승리로 다시 올라간다(결정표 10)
    assert KIND_UPLOAD_NEW in by_key.get(old, []), by_key


def test_forget_when_gone_from_both_sides():
    k = path_key("a.txt")
    d, _ = diff(base={k: B("a.txt")}, local={},
                remote=RemoteView(is_complete=True, deleted_keys={k}))
    assert kinds(d) == [(0, KIND_FORGET)]


def test_no_baseline_is_protected_not_overwritten():
    """기준선이 없으면 어느 쪽이 최신인지 모른다 — 어느 쪽도 덮지 않는다."""
    k = path_key("a.txt")
    rec = FileRecord(drive_id="d", rel_path="a.txt", file_id="F1", local_md5=None,
                     remote_version=1)
    d, _ = diff(base={k: rec}, local={k: L("a.txt", "aa")},
                remote=RemoteView(is_complete=True, entries={k: R("a.txt", "aa")}))
    assert [x.kind for x in d] == ["PROTECT"]


# =========================================================== I1: 불완전 뷰
def test_delta_view_never_implies_remote_deletion():
    """델타 뷰에서 '안 보인다'는 삭제가 아니다. 이걸 어기면 폴더 개명에 하위가 다 날아간다."""
    k = path_key("a.txt")
    d, stats = diff(base={k: B("a.txt")}, local={k: L("a.txt", "aa")},
                    remote=RemoteView(is_complete=False), propagate_deletes=True)
    assert d == []
    assert stats.skipped_unobserved == 1


def test_full_view_absence_is_deletion():
    """반대로 전체 뷰에서 없으면 삭제가 맞다(전파 켜짐일 때만 실행)."""
    k = path_key("a.txt")
    d, _ = diff(base={k: B("a.txt")}, local={k: L("a.txt", "aa")},
                remote=RemoteView(is_complete=True), propagate_deletes=True)
    assert kinds(d) == [(8, KIND_LOCAL_TRASH)]


def test_md5_probe_budget_is_reported_not_silently_skipped():
    k = path_key("a.txt")
    d, stats = diff(base={}, local={k: L("a.txt", "aa", size=10)},
                    remote=RemoteView(is_complete=True, entries={k: R("a.txt", None, size=10)}),
                    md5_probe=lambda r: "aa", md5_probe_budget=0)
    assert stats.md5_probe_skipped == 1
    assert kinds(d) == [(3, KIND_CONFLICT)]      # 모르면 안전한 쪽(양쪽 보존)


# =========================================================== planner 안전 게이트
def _decisions_for_trash(n: int):
    from dooray_sync.core.differ import Decision
    return [Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path=f"f{i}.txt",
                     key=path_key(f"f{i}.txt")) for i in range(n)]


def test_bulk_delete_aborts_before_executing_anything():
    try:
        build_plan(_decisions_for_trash(60), base_count=1000, p=Profile())
    except BulkDeleteAbort as exc:
        assert "60건" in str(exc)
        return
    raise AssertionError("대량 삭제 임계를 넘겼는데 중단하지 않았다")


def test_bulk_delete_ratio_threshold():
    try:
        build_plan(_decisions_for_trash(6), base_count=20, p=Profile())
    except BulkDeleteAbort:
        return
    raise AssertionError("비율 임계(20%)를 넘겼는데 중단하지 않았다")


def test_ratio_threshold_does_not_fire_on_tiny_baselines():
    """작은 폴더에서 비율 임계가 정상 삭제까지 막으면 사용자가 습관적으로
    --allow-bulk-delete 를 붙이게 되고 안전장치가 통째로 무력화된다.

    실계정에서 확인: 파일 5개짜리 시험 프로파일에서 1건 삭제가 '20%'에 걸려 막혔다.
    """
    # 기준선 5건에서 1건 삭제 — 막히면 안 된다
    pl = build_plan(_decisions_for_trash(1), base_count=5, p=Profile(),
                    base_keys=[path_key(f"f{i}.txt") for i in range(5)])
    assert pl.delete_count == 1

    # 기준선이 충분히 크면 비율 임계는 그대로 동작해야 한다
    try:
        build_plan(_decisions_for_trash(6), base_count=20, p=Profile())
    except BulkDeleteAbort:
        return
    raise AssertionError("충분한 기준선에서는 비율 임계가 동작해야 한다")


def test_folder_trash_absorbs_descendants():
    """폴더 휴지통은 하위에 재귀 적용된다 — 자식마다 다시 지우려 하면 실패한다(C5)."""
    from dooray_sync.core.differ import Decision
    ds = [
        Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="d", key=path_key("d"), is_dir=True),
        Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="d/x.txt", key=path_key("d/x.txt")),
    ]
    pl = build_plan(ds, base_count=1000, p=Profile(bulk_delete_abort_count=0,
                                                   bulk_delete_abort_ratio=0))
    assert [a.rel_path for a in pl.actions] == ["d"]


def test_local_trash_degrades_to_report_without_send2trash():
    """send2trash가 없으면 os.remove로 내려가지 않고 '보고'로 강등한다(규약_M2 I5)."""
    pl = build_plan(_decisions_for_trash(1), trash_ok=False, trash_reason="없음")
    assert pl.actions == [] and len(pl.reports) == 1


def test_plan_orders_parents_before_children():
    from dooray_sync.core.differ import Decision
    ds = [
        Decision(case=2, kind=KIND_DOWNLOAD_NEW, rel_path="a/b/c.txt", key=path_key("a/b/c.txt")),
        Decision(case=2, kind=KIND_MKDIR_LOCAL, rel_path="a/b", key=path_key("a/b"), is_dir=True),
        Decision(case=2, kind=KIND_MKDIR_LOCAL, rel_path="a", key=path_key("a"), is_dir=True),
    ]
    pl = build_plan(ds)
    assert [a.rel_path for a in pl.actions] == ["a", "a/b", "a/b/c.txt"]


def test_conflict_copy_name_format():
    when = _dt.datetime(2026, 8, 2, 15, 30)
    assert conflict_copy_name("a/문서.docx", when) == "a/문서 (충돌 2026-08-02 1530).docx"
    assert conflict_copy_name("README", when) == "README (충돌 2026-08-02 1530)"
    # UT-10(2026-08-04): 점으로 시작하는 이름은 태그를 끝에 붙인다. 점 앞에 끼우면
    # ' (충돌 …).gitignore'처럼 앞공백 이름이 되어 서버가 절삭(R14) — 저장명이
    # 로컬과 어긋나 다음 sync가 개명 왕복을 한 번 더 돈다.
    assert conflict_copy_name(".gitignore", when) == ".gitignore (충돌 2026-08-02 1530)"
    from dooray_sync.util.paths import name_issue, server_name_will_differ
    for src in ("a/문서.docx", "README", ".gitignore", "b/.env"):
        copy_name = conflict_copy_name(src, when).rpartition("/")[2]
        assert name_issue(copy_name) is None, copy_name
        assert not server_name_will_differ(copy_name), copy_name


# =========================================================== B4: 폴더 개명 하위 재열람
class _FakeApiClient:
    """목록 + changes를 함께 서비스하는 최소 클라이언트."""

    def __init__(self) -> None:
        import logging
        self.logger = logging.getLogger("fake")
        self.nodes: dict[str, dict] = {}
        self.changes: list[dict] = []
        self.list_calls = 0

    def add(self, fid: str, name: str, parent: str, type_: str = "file",
            version: int = 1, size: int = 10, md5: str | None = None) -> None:
        self.nodes[fid] = {"id": fid, "name": name, "type": type_, "parentId": parent,
                           "version": version, "size": size, "hash": md5}

    def push_change(self, fid: str, path: str, *, deleted: bool = False,
                    revision: int = 1) -> None:
        n = self.nodes.get(fid, {})
        if deleted:
            self.changes.append({"changeType": "deleted", "revision": str(revision),
                                 "file": {"id": fid, "type": n.get("type", "file"),
                                          "revision": str(revision)}})
            return
        self.changes.append({"changeType": "updated", "revision": str(revision), "file": {
            "id": fid, "type": n.get("type", "file"), "version": n.get("version", 1),
            "name": n.get("name"), "path": path, "hash": n.get("hash"),
            "size": n.get("size")}})

    def api(self, method, path, **kw):
        params = kw.get("params") or {}
        if "/changes" in path:
            after = int(params.get("latestRevision") or 0)
            rest = [c for c in self.changes if int(c["revision"]) > after]
            return {"header": {"isSuccessful": True}, "result": rest[:int(params.get("size") or 200)]}
        if params.get("subTypes") == "root":
            return {"header": {"isSuccessful": True},
                    "result": [{"id": "root", "name": "root", "type": "folder",
                                "subType": "root"}]}
        self.list_calls += 1
        parent = params.get("parentId") or "root"
        kids = [n for n in self.nodes.values() if n["parentId"] == parent]
        page, size = int(params.get("page") or 0), int(params.get("size") or 100)
        return {"header": {"isSuccessful": True},
                "result": kids[page * size:(page + 1) * size], "totalCount": len(kids)}


def test_b4_folder_rename_relists_subtree_and_causes_no_deletion():
    """R8: 폴더를 개명해도 하위 파일에는 changes 이벤트가 오지 않는다.

    하위 재열람을 빠뜨리면 하위 전체가 '원격에서 사라짐'으로 보여 대량 오삭제가 된다.
    이 테스트는 **삭제 판정이 0건**이고 하위가 이동으로 잡히는 것을 확인한다.
    """
    client = _FakeApiClient()
    client.add("DIR", "새이름", "root", type_="folder")
    client.add("F1", "a.txt", "DIR", md5="aa")
    client.add("F2", "b.txt", "DIR", md5="bb")
    client.push_change("DIR", "/", revision=10)      # 폴더만 이벤트가 온다

    drive = DriveAPI(client)
    collector = RemoteCollector(drive, "d", "", "root")
    view = collector.delta(Cursor(revision=0),
                           known_by_file_id={"DIR": "옛이름", "F1": "옛이름/a.txt",
                                             "F2": "옛이름/b.txt"})

    assert view.subtrees_relisted == 1, "폴더 updated에 하위 재열람이 일어나지 않았다"
    assert set(view.entries) == {path_key("새이름"), path_key("새이름/a.txt"),
                                 path_key("새이름/b.txt")}
    assert view.deleted_keys == set()

    base = {
        path_key("옛이름"): B("옛이름", fid="DIR", is_dir=True),
        path_key("옛이름/a.txt"): B("옛이름/a.txt", md5="aa", fid="F1"),
        path_key("옛이름/b.txt"): B("옛이름/b.txt", md5="bb", fid="F2"),
    }
    local = {
        path_key("옛이름"): L("옛이름", is_dir=True),
        path_key("옛이름/a.txt"): L("옛이름/a.txt", "aa"),
        path_key("옛이름/b.txt"): L("옛이름/b.txt", "bb"),
    }
    decisions, _ = diff(base=base, local=local, remote=view, propagate_deletes=True)
    deletes = [d for d in decisions if d.kind in (KIND_LOCAL_TRASH, KIND_REMOTE_TRASH)]
    assert deletes == [], f"오삭제 판정 {len(deletes)}건: {[d.rel_path for d in deletes]}"
    assert sum(1 for d in decisions if d.kind == KIND_LOCAL_MOVE) == 3


def test_deleted_change_for_unknown_id_is_ignored():
    """실측: 모르는 id의 deleted가 정상적으로 온다(B3). 오류로 다루면 안 된다."""
    client = _FakeApiClient()
    client.push_change("모르는id", "/", deleted=True, revision=5)
    view = RemoteCollector(DriveAPI(client), "d", "", "root").delta(Cursor(), known_by_file_id={})
    assert view.deleted_keys == set() and view.changes_seen == 1


def test_changes_paging_terminates_only_on_empty_page():
    """R11 재확인 — 델타 수집 경로에서도 부분 페이지에 속으면 안 된다."""
    client = _FakeApiClient()
    for i in range(250):
        client.add(f"F{i}", f"f{i}.txt", "root", md5="aa")
        client.push_change(f"F{i}", "/", revision=i + 1)
    view = RemoteCollector(DriveAPI(client), "d", "", "root").delta(Cursor())
    assert view.changes_seen == 250, f"{view.changes_seen}건만 수집됨"


# =========================================================== 실행기: 원격 대역
class _FakeDrive:
    """DriveAPI 대역 — 메모리 원격 + 실제 로컬 파일시스템."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {"root": {"id": "root", "name": "", "type": "folder",
                                                "parent": "", "content": b"", "version": 0}}
        self.seq = 0
        self.trashed: set[str] = set()
        self.fail_download_once = False

    # --- 내부 ---
    def _new_id(self, prefix: str = "N") -> str:
        self.seq += 1
        return f"{prefix}{self.seq}"

    def _rf(self, n: dict) -> RemoteFile:
        return RemoteFile(id=n["id"], name=n["name"], type=n["type"],
                          parent_id=n["parent"], version=n["version"],
                          size=len(n["content"]) if n["type"] == "file" else None,
                          md5=_md5(n["content"]) if n["type"] == "file" else None)

    def put(self, name: str, parent: str, content: bytes = b"", type_: str = "file") -> str:
        fid = self._new_id()
        self.nodes[fid] = {"id": fid, "name": name, "type": type_, "parent": parent,
                           "content": content, "version": 1}
        return fid

    def alive(self, parent: str):
        return [n for n in self.nodes.values()
                if n["parent"] == parent and n["id"] not in self.trashed]

    # --- DriveAPI 인터페이스 ---
    def iter_children(self, drive_id, parent_id, size=100):
        for n in self.alive(parent_id):
            yield self._rf(n)

    def find_child_by_name(self, drive_id, parent_id, name):
        for n in self.alive(parent_id):
            if n["name"] == name:
                return self._rf(n)
        for n in self.alive(parent_id):
            if path_key(n["name"]) == path_key(name):
                return self._rf(n)
        return None

    def create_folder_ex(self, drive_id, parent_id, name):
        found = self.find_child_by_name(drive_id, parent_id, name)
        if found is not None:
            return found, False
        return self._rf(self.nodes[self.put(name, parent_id, type_="folder")]), True

    def create_folder_full(self, drive_id, parent_id, name):
        return self.create_folder_ex(drive_id, parent_id, name)[0]

    def upload_new(self, drive_id, parent_id, filename, local_path):
        if self.find_child_by_name(drive_id, parent_id, filename) is not None:
            raise DoorayApiError("Duplicate request", status=409)
        with open(ext_path(local_path), "rb") as f:
            data = f.read()
        return self._rf(self.nodes[self.put(filename, parent_id, data)])

    def upload_version(self, drive_id, file_id, filename, local_path):
        with open(ext_path(local_path), "rb") as f:
            data = f.read()
        n = self.nodes[file_id]
        n["content"] = data
        n["version"] += 1
        return {"id": file_id, "version": n["version"]}

    def download(self, drive_id, file_id, dest, *, expected_size=None,
                 expected_md5=None, pre_replace_guard=None, on_tmp=None):
        # 실제 DriveAPI.download와 같은 순서로 동작해야 한다: 임시파일 경로를 먼저
        # 정하고 on_tmp로 알린 뒤 전송한다. 페이크가 여기서 다르면 '내가 믿는 서버'만
        # 검증하게 된다(교훈 §27).
        data = self.nodes[file_id]["content"]
        dest = Path(dest)
        tmp_dir = dest.parent / ".dooraysync_tmp"
        os.makedirs(ext_path(tmp_dir), exist_ok=True)
        tmp = tmp_dir / f"{file_id}.part"
        if on_tmp is not None:
            on_tmp(tmp)
        if self.fail_download_once:
            self.fail_download_once = False
            # 실제 전송 중단처럼 **찌꺼기를 남긴 채** 실패한다
            with open(ext_path(tmp), "wb") as f:
                f.write(data[: len(data) // 2])
            raise DoorayApiError("전송 중 끊김(시뮬레이션)")
        with open(ext_path(tmp), "wb") as f:
            f.write(data)
        if pre_replace_guard is not None:
            try:
                pre_replace_guard()
            except BaseException:
                os.remove(ext_path(tmp))
                raise
        os.replace(ext_path(tmp), ext_path(dest))
        return {"bytes": len(data), "md5": _md5(data), "redirect_host": None}

    def remote_md5(self, drive_id, file_id, tmp_dir):
        return _md5(self.nodes[file_id]["content"])

    def get_file_meta(self, drive_id, file_id):
        # 실제 meta 응답은 parentFile.path를 담는다(models.py 실측 주석) — 부모 체인으로
        # 재현한다. 이걸 빼먹으면 앵커 추종 테스트가 '내가 믿는 서버'만 검증하게 된다.
        n = self.nodes[file_id]
        parts: list[str] = []
        cur = n["parent"]
        while cur and cur != "root":
            parts.append(self.nodes[cur]["name"])
            cur = self.nodes[cur]["parent"]
        parent_path = "/" + "/".join(reversed(parts)) if parts else "/"
        return dataclasses.replace(self._rf(n), parent_path=parent_path)

    def move(self, drive_id, file_id, destination_file_id):
        if destination_file_id == "trash":
            self.trashed.add(file_id)
            return
        self.nodes[file_id]["parent"] = destination_file_id

    def rename(self, drive_id, file_id, new_name):
        self.nodes[file_id]["name"] = new_name

    def move_to_trash(self, drive_id, file_id):
        if file_id in self.trashed:
            raise DoorayApiError("이미 휴지통", status=200,
                                 result_code=NO_ACCESS_AUTHORITY)
        self.trashed.add(file_id)

    def find_root_folder(self, drive_id):
        return "root"

    def advance_to_tip(self, drive_id, cursor):
        return Cursor(revision=0)

    def walk(self, drive_id, root_id, base_path=""):
        queue = [(root_id, base_path or "/")]
        while queue:
            folder, cur = queue.pop(0)
            for n in self.alive(folder):
                rf = self._rf(n)
                full = (cur.rstrip("/") + "/" + n["name"]) if cur != "/" else "/" + n["name"]
                yield rf, full
                if n["type"] == "folder":
                    queue.append((n["id"], full))


def _setup(tmp_path: Path):
    root = tmp_path / "local"
    os.makedirs(ext_path(root), exist_ok=True)
    store = Store(tmp_path / "s.db")
    p = Profile(name="t", drive_id="d", local_root=str(root))
    return root, store, p


def _write(root: Path, rel: str, data: bytes) -> LocalEntry:
    path = Path(str(root) + "\\" + rel.replace("/", "\\"))
    os.makedirs(ext_path(path.parent), exist_ok=True)
    with open(ext_path(path), "wb") as f:
        f.write(data)
    st = os.stat(ext_path(path))
    return LocalEntry(rel_path=rel, rel_path_key=path_key(rel), is_dir=False,
                      disk_path=str(path), mtime_ns=st.st_mtime_ns, size=st.st_size,
                      md5=_md5(data))


def _read(root: Path, rel: str) -> bytes:
    with open(ext_path(Path(str(root) + "\\" + rel.replace("/", "\\"))), "rb") as f:
        return f.read()


def test_conflict_preserves_both_sides(tmp_path: Path):
    """결정표 6 실행: 로컬 편집이 살아 있어야 하고, 원격본도 제자리에 있어야 한다."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("a.txt", "root", b"REMOTE-NEW")
    local_entry = _write(root, "a.txt", b"LOCAL-EDIT")

    base = {path_key("a.txt"): B("a.txt", md5=_md5(b"BASE"), fid=fid)}
    view = RemoteView(is_complete=True, entries={
        path_key("a.txt"): R("a.txt", _md5(b"REMOTE-NEW"), ver=2, size=10, fid=fid)})
    decisions, _ = diff(base=base, local={path_key("a.txt"): local_entry}, remote=view)
    assert [d.kind for d in decisions] == [KIND_CONFLICT]

    pl = build_plan(decisions, base_count=1, p=p)
    ex = SyncExecutor(drive, store, p, dict(base), SyncJournal(store),
                      root_id="root", now=lambda: _dt.datetime(2026, 8, 2, 15, 30))
    rep = ex.run(pl)

    assert not rep.failures, rep.failures
    assert _read(root, "a.txt") == b"REMOTE-NEW", "원격본이 원래 경로에 없다"
    copy_rel = conflict_copy_name("a.txt", _dt.datetime(2026, 8, 2, 15, 30))
    assert _read(root, copy_rel) == b"LOCAL-EDIT", "로컬 편집이 사라졌다"
    assert len(list(store.iter_unresolved())) == 1
    # 사본이 원격에도 올라간다(기본 설정)
    assert drive.find_child_by_name("d", "root", copy_rel) is not None
    # 나간 바이트가 집계돼야 한다 — 0으로 보고하면 데이터가 안 나간 것으로 읽힌다
    assert rep.bytes_up >= len(b"LOCAL-EDIT"), f"충돌 사본 업로드가 집계되지 않았다: {rep.bytes_up}"
    store.close()


def test_upload_new_never_overwrites_unknown_remote_file(tmp_path: Path):
    """'신규 업로드'인데 원격에 같은 이름이 이미 있으면 덮어쓰지 않는다."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    drive.put("a.txt", "root", b"SOMEONE-ELSE")
    entry = _write(root, "a.txt", b"MINE")

    from dooray_sync.core.differ import Decision
    d = Decision(case=1, kind=KIND_UPLOAD_NEW, rel_path="a.txt", key=path_key("a.txt"),
                 local=entry)
    ex = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root")
    rep = ex.run(build_plan([d], p=p))

    assert not rep.failures
    assert len(rep.protected) == 1
    remote = drive.find_child_by_name("d", "root", "a.txt")
    assert drive.nodes[remote.id]["content"] == b"SOMEONE-ELSE", "남의 파일을 덮어썼다"
    store.close()


def test_crash_during_download_converges_without_loss(tmp_path: Path):
    """M2 완료 기준: 전송 중 중단 → 재실행 → 무손실 수렴.

    1차 실행에서 다운로드가 끊기고, 복구 후 2차 실행이 정상적으로 받아 내용이 일치해야 한다.
    중간에 로컬 원본이 손상되거나 base가 '받은 셈'으로 기록되면 안 된다.
    """
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("a.txt", "root", b"REMOTE-CONTENT")
    view = RemoteView(is_complete=True, entries={
        path_key("a.txt"): R("a.txt", _md5(b"REMOTE-CONTENT"), size=14, fid=fid)})

    decisions, _ = diff(base={}, local={}, remote=view)
    pl = build_plan(decisions, p=p)

    drive.fail_download_once = True
    ex = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root")
    rep = ex.run(pl)
    assert len(rep.failures) == 1, "끊긴 전송이 실패로 집계되지 않았다"

    # base가 '받은 것처럼' 기록되면 안 된다
    rec = store.get_by_path("d", "a.txt")
    assert rec is not None and rec.local_md5 is None and rec.sync_status == "error"

    # 중단된 전송은 찌꺼기를 남긴다 — 복구가 그걸 지워야 한다
    assert list(root.rglob(".dooraysync_tmp/*.part")), "이 테스트의 전제(찌꺼기)가 깨졌다"

    # 복구 — 미완료 저널을 정리하고 재검사 대상으로 표시만 한다
    report = recover(store, "d", root)
    assert report.scanned >= 1
    assert list(store.iter_incomplete()) == [], "복구 후에도 미완료 저널이 남았다"
    assert not list(root.rglob(".dooraysync_tmp/*.part")), (
        "복구가 임시파일을 지우지 못했다 — 실계정에서 실제로 남았던 결함")
    assert report.tmp_removed >= 1
    after = store.get_by_path("d", "a.txt")
    assert after.local_md5 is None, "복구가 base를 추정으로 채웠다"

    # 2차 실행 — 이번에는 성공하고 내용이 정확히 일치한다
    ex2 = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root")
    rep2 = ex2.run(build_plan(diff(base={}, local={}, remote=view)[0], p=p))
    assert not rep2.failures
    assert _read(root, "a.txt") == b"REMOTE-CONTENT"
    final = store.get_by_path("d", "a.txt")
    assert final.sync_status == "synced" and final.local_md5 == _md5(b"REMOTE-CONTENT")
    store.close()


def test_recover_removes_only_our_temp_files(tmp_path: Path):
    """복구는 임시파일만 지운다. 사용자 파일 경로가 들어와도 지우지 않는다."""
    root, store, p = _setup(tmp_path)
    victim = _write(root, "소중한.txt", b"KEEP")
    journal = SyncJournal(store)
    jid = journal.begin("DOWNLOAD_NEW", "소중한.txt", detail={"tmp": victim.disk_path})
    journal.phase(jid, "started")

    recover(store, "d", root)
    assert _read(root, "소중한.txt") == b"KEEP", "사용자 파일을 지웠다"
    store.close()


def test_download_guard_protects_file_modified_mid_transfer(tmp_path: Path):
    """C2: os.replace 직전 재-stat. 전송 중 저장된 편집을 덮지 않는다."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("a.txt", "root", b"REMOTE")
    entry = _write(root, "a.txt", b"OLD")

    real_download = drive.download

    def racing_download(*a, **kw):
        # 전송 도중 사용자가 저장한 상황을 만든다
        _write(root, "a.txt", b"USER-EDIT-DURING-TRANSFER")
        return real_download(*a, **kw)

    drive.download = racing_download

    from dooray_sync.core.differ import Decision
    d = Decision(case=5, kind=KIND_DOWNLOAD_UPDATE, rel_path="a.txt", key=path_key("a.txt"),
                 local=entry,
                 remote=R("a.txt", _md5(b"REMOTE"), ver=2, size=6, fid=fid))
    ex = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root")
    rep = ex.run(build_plan([d], p=p))

    assert _read(root, "a.txt") == b"USER-EDIT-DURING-TRANSFER", "전송 중 편집이 덮였다"
    assert len(rep.protected) == 1 and not rep.failures
    store.close()


def test_remote_trash_tolerates_already_trashed(tmp_path: Path):
    """폴더를 휴지통에 보내면 하위가 함께 들어간다 — 자식 move는 '이미 처리됨'으로 관용."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("a.txt", "root", b"x")
    drive.move_to_trash("d", fid)

    from dooray_sync.core.differ import Decision
    d = Decision(case=7, kind=KIND_REMOTE_TRASH, rel_path="a.txt", key=path_key("a.txt"),
                 base=B("a.txt", fid=fid))
    ex = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root")
    rep = ex.run(build_plan([d], p=Profile(name="t", drive_id="d", local_root=str(root),
                                           bulk_delete_abort_count=0,
                                           bulk_delete_abort_ratio=0)))
    assert not rep.failures, rep.failures
    store.close()


def test_local_move_updates_subtree_records(tmp_path: Path):
    """폴더 이동 시 하위 레코드 경로도 같이 옮겨야 한다 — 남으면 다음 패스가 오삭제로 읽는다."""
    _root, store, _p = _setup(tmp_path)
    for rel in ("옛/a.txt", "옛/sub/b.txt"):
        store.upsert_file(FileRecord(drive_id="d", rel_path=rel, file_id=rel))
    store.upsert_file(FileRecord(drive_id="d", rel_path="옛", file_id="DIR", is_dir=True))

    moved = store.move_subtree("d", "옛", "새")
    assert moved == 3
    assert {r.rel_path for r in store.iter_files("d")} == {"새", "새/a.txt", "새/sub/b.txt"}
    store.close()


# =========================================================== CLI 통합
def test_cli_sync_end_to_end(tmp_path: Path):
    """`dsync sync` 한 번으로 양방향이 실제로 맞물리는지 — 배선 전체를 지나가는 테스트.

    로컬 신규는 올라가고, 원격 신규는 내려오고, 같은 내용은 전송되지 않아야 한다.
    """
    import contextlib

    from dooray_sync import config as cfg
    from dooray_sync.cli import main as cli

    os.environ[cfg.ENV_CONFIG_DIR] = str(tmp_path / "cfg")
    os.environ[cfg.ENV_STATE_DIR] = str(tmp_path / "state")
    os.environ["DOORAY_API_TOKEN"] = "테스트토큰" + "x" * 30
    root = tmp_path / "local"
    os.makedirs(ext_path(root), exist_ok=True)

    drive = _FakeDrive()
    drive.put("원격만.txt", "root", b"FROM-REMOTE")
    drive.put("양쪽같음.txt", "root", b"SAME")
    _write(root, "로컬만.txt", b"FROM-LOCAL")
    _write(root, "양쪽같음.txt", b"SAME")

    @contextlib.contextmanager
    def fake_api(p, log):
        yield drive

    real_api = cli._drive_api
    cli._drive_api = fake_api
    try:
        cfg.save_config(cfg.Profile(name="e2e", drive_id="d", local_root=str(root)))
        try:
            cli.sync(profile="e2e", dry_run=False, full=True, propagate_deletes=False,
                     allow_bulk_delete=False, md5_probes=200, verbose=False)
        except SystemExit as exc:      # typer.Exit
            assert getattr(exc, "code", 0) in (0, None), f"sync 실패(exit={exc.code})"
    finally:
        cli._drive_api = real_api
        for k in (cfg.ENV_CONFIG_DIR, cfg.ENV_STATE_DIR, "DOORAY_API_TOKEN"):
            os.environ.pop(k, None)

    # 원격 신규가 로컬에 내려왔다
    assert _read(root, "원격만.txt") == b"FROM-REMOTE"
    # 로컬 신규가 원격에 올라갔다
    up = drive.find_child_by_name("d", "root", "로컬만.txt")
    assert up is not None and drive.nodes[up.id]["content"] == b"FROM-LOCAL"
    # 같은 내용은 새 버전을 만들지 않았다(전송 없음)
    same = drive.find_child_by_name("d", "root", "양쪽같음.txt")
    assert drive.nodes[same.id]["version"] == 1, "내용이 같은데 재업로드했다"


def test_resolve_anchored_follows_rename():
    """원격 루트가 개명돼 경로 해석이 실패하면 앵커(folder_id)로 되찾는다."""
    drive = _FakeDrive()
    work = drive.put("WORK", "root", type_="folder")
    target = drive.put("옛이름", work, type_="folder")
    drive.rename("d", target, "새이름")
    res = resolve_remote_root_anchored(drive, "d", "WORK/옛이름", anchor_id=target)
    assert res.followed and res.root_id == target and res.prefix == "WORK/새이름"


def test_resolve_anchored_refuses_trashed_anchor():
    """앵커가 휴지통에 있으면 추종하지 않고 원래 오류를 낸다 — 재해석이 생존 게이트다."""
    drive = _FakeDrive()
    work = drive.put("WORK", "root", type_="folder")
    target = drive.put("옛이름", work, type_="folder")
    drive.rename("d", target, "새이름")
    drive.trashed.add(target)
    try:
        resolve_remote_root_anchored(drive, "d", "WORK/옛이름", anchor_id=target)
    except RemoteRootError:
        pass
    else:
        raise AssertionError("휴지통 앵커를 추종했다")


def test_sync_follows_renamed_remote_root_and_updates_config(tmp_path: Path):
    """원격 루트 개명 시: dry-run은 앵커로 계획만 내고 설정을 안 바꾸며,
    실제 실행은 알림과 함께 config.toml의 remote_path를 자동 갱신한다.
    앵커는 첫 성공 해석에서 지연 백필된다(기존 프로파일 마이그레이션 불필요)."""
    import contextlib

    from dooray_sync import config as cfg
    from dooray_sync.cli import main as cli
    from dooray_sync.store.db import META_REMOTE_ROOT_ID

    os.environ[cfg.ENV_CONFIG_DIR] = str(tmp_path / "cfg")
    os.environ[cfg.ENV_STATE_DIR] = str(tmp_path / "state")
    os.environ["DOORAY_API_TOKEN"] = "테스트토큰" + "x" * 30
    root = tmp_path / "local"
    os.makedirs(ext_path(root), exist_ok=True)

    drive = _FakeDrive()
    work = drive.put("WORK", "root", type_="folder")
    target = drive.put("대상", work, type_="folder")
    drive.put("a.txt", target, b"DATA")

    @contextlib.contextmanager
    def fake_api(p, log):
        yield drive

    def run_sync(**kw):
        try:
            cli.sync(profile="anch", propagate_deletes=False,
                     allow_bulk_delete=False, md5_probes=200, verbose=False, **kw)
        except (SystemExit, typer.Exit) as exc:
            code = getattr(exc, "code", 0) or getattr(exc, "exit_code", 0)
            assert code in (0, None), f"sync 실패(exit={code})"

    real_api = cli._drive_api
    cli._drive_api = fake_api
    try:
        cfg.save_config(cfg.Profile(name="anch", drive_id="d",
                                    local_root=str(root), remote_path="WORK/대상"))
        run_sync(dry_run=False, full=True)
        assert _read(root, "a.txt") == b"DATA"
        with Store(cli.db_path("anch")) as store:
            assert store.get_meta(META_REMOTE_ROOT_ID) == target, "앵커 백필 실패"

        drive.rename("d", target, "대상개명")

        # dry-run: 앵커로 추종해 계획은 내되, 설정·meta는 그대로
        run_sync(dry_run=True, full=True)
        assert cfg.load_config("anch").remote_path == "WORK/대상", "dry-run이 설정을 바꿨다"

        # 실제 실행: remote_path 자동 갱신
        run_sync(dry_run=False, full=True)
        assert cfg.load_config("anch").remote_path == "WORK/대상개명"
    finally:
        cli._drive_api = real_api
        for k in (cfg.ENV_CONFIG_DIR, cfg.ENV_STATE_DIR, "DOORAY_API_TOKEN"):
            os.environ.pop(k, None)


def test_tool_file_is_never_scanned_locally(tmp_path: Path):
    """synchere.bat(폴더 동기화 도구)은 로컬 스캔에서 항상 제외된다 — 도구 자신이
    전송 대상이 되면 안 된다(2026-08-07 사용자 요구). 하위 폴더에 있어도 같다."""
    root, _store, _p = _setup(tmp_path)
    _write(root, "synchere.bat", b"@echo off")
    _write(root, "sub/synchere.bat", b"@echo off")
    _write(root, "normal.txt", b"DATA")
    entries = LocalScanner(root, []).scan()
    assert path_key("normal.txt") in entries
    assert path_key("synchere.bat") not in entries
    assert path_key("sub/synchere.bat") not in entries


def test_tool_file_is_excluded_from_remote_view(tmp_path: Path):
    """원격에 올라간 synchere.bat(과거 업로드 잔재 등)도 뷰에서 제외된다 —
    로컬만 제외하면 '원격 신규'로 보여 매번 되받으려 든다."""
    drive = _FakeDrive()
    drive.put("synchere.bat", "root", b"@echo off")
    drive.put("normal.txt", "root", b"DATA")
    view = RemoteCollector(drive, "d", "", root_id="root").full()
    assert path_key("normal.txt") in view.entries
    assert path_key("synchere.bat") not in view.entries


def test_reconcile_finds_remote_counterpart_missing_from_db(tmp_path: Path):
    """UT-04(2026-08-04 사용자 테스트): init 이후 원격에 생긴 파일은 DB에 기록이 없어
    reconcile이 '대조 대상 0건'으로 끝났다 — pull이 '기준선 없음 → reconcile로
    대조하세요'라고 안내한 바로 그 상태를 reconcile이 못 봤다(영구 루프).
    이제 DB에 없는 로컬 파일은 원격 walk로 상대를 찾아 대조한다."""
    import contextlib

    from dooray_sync import config as cfg
    from dooray_sync.cli import main as cli

    os.environ[cfg.ENV_CONFIG_DIR] = str(tmp_path / "cfg")
    os.environ[cfg.ENV_STATE_DIR] = str(tmp_path / "state")
    os.environ["DOORAY_API_TOKEN"] = "테스트토큰" + "x" * 30
    root = tmp_path / "local"
    os.makedirs(ext_path(root), exist_ok=True)

    drive = _FakeDrive()
    same_id = drive.put("둘다같음.txt", "root", b"SAME")
    drive.put("둘다다름.txt", "root", b"REMOTE")
    _write(root, "둘다같음.txt", b"SAME")
    _write(root, "둘다다름.txt", b"LOCAL")
    _write(root, "로컬만.txt", b"ONLY-LOCAL")

    @contextlib.contextmanager
    def fake_api(p, log):
        yield drive

    real_api = cli._drive_api
    cli._drive_api = fake_api
    try:
        cfg.save_config(cfg.Profile(name="rec", drive_id="d", local_root=str(root)))
        try:
            cli.reconcile(profile="rec", dry_run=False, trust_size=False, verbose=False)
        except (SystemExit, typer.Exit) as exc:
            code = getattr(exc, "code", 0) or getattr(exc, "exit_code", 0)
            assert code in (0, None), f"reconcile 실패(exit={code})"
        with Store(cli.db_path("rec")) as store:
            base = store.all_by_key("d")
    finally:
        cli._drive_api = real_api
        for k in (cfg.ENV_CONFIG_DIR, cfg.ENV_STATE_DIR, "DOORAY_API_TOKEN"):
            os.environ.pop(k, None)

    # 내용이 같은 쌍: 전송 없이 기준선이 기록됐다
    rec_same = base.get(path_key("둘다같음.txt"))
    assert rec_same is not None and rec_same.local_md5, "원격 상대를 찾아 기준선을 기록해야 한다"
    assert rec_same.file_id == same_id
    # 내용이 다른 쌍: 기준선을 기록하지 않는다(사용자 판단)
    rec_diff = base.get(path_key("둘다다름.txt"))
    assert rec_diff is None or not rec_diff.local_md5, "내용이 다른데 기준선을 기록했다"
    # 원격에 상대가 없는 로컬 신규는 reconcile 대상이 아니다(push 몫)
    assert base.get(path_key("로컬만.txt")) is None


# =========================================================== 적대 검증에서 나온 결함 회귀
def test_diff_writes_computed_md5_back_into_entry():
    """fill_md5는 **새 객체**를 돌려준다. 반환값만 쓰고 원본에 되쓰지 않으면
    Decision.local.md5가 None으로 남아 executor가 base에 local_md5=NULL을 기록하고,
    그 파일은 다음 실행부터 영원히 PROTECT만 된다(업로드가 1회용이 된다)."""
    import dataclasses
    entry = LocalEntry(rel_path="a.txt", rel_path_key=path_key("a.txt"), is_dir=False,
                       disk_path="", mtime_ns=111, size=10, md5=None)
    k = path_key("a.txt")
    rec = B("a.txt", md5="old", mtime=999)          # 메타가 달라 해시 계산을 유발
    diff(base={k: rec}, local={k: entry},
         remote=RemoteView(is_complete=False),
         hash_local=lambda e: dataclasses.replace(e, md5="newmd5"))
    assert entry.md5 == "newmd5", "계산한 해시가 원본 entry에 반영되지 않았다"


def test_upload_records_baseline_so_next_sync_is_not_protected(tmp_path: Path):
    """업로드 후 base에 local_md5가 남아야 다음 실행이 정상 판정한다."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    entry = _write(root, "a.txt", b"HELLO")
    entry.md5 = None                                 # 스캔 직후 상태(해시 미계산)

    scanner = LocalScanner(root, [])
    decisions, _ = diff(base={}, local={path_key("a.txt"): entry},
                        remote=RemoteView(is_complete=True),
                        hash_local=scanner.fill_md5)
    ex = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root")
    rep = ex.run(build_plan(decisions, p=p))

    assert not rep.failures
    rec = store.get_by_path("d", "a.txt")
    assert rec.local_md5 == _md5(b"HELLO"), "업로드 후 기준선이 비어 다음 실행이 막힌다"
    assert rec.sync_status == "synced"
    store.close()


def test_bulk_delete_gate_counts_folder_subtree_not_actions():
    """폴더 삭제 1건이 하위 수천 건을 지운다 — 게이트가 Action 수를 세면 무력화된다."""
    from dooray_sync.core.differ import Decision
    d = Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="Work", key=path_key("Work"),
                 is_dir=True)
    base_keys = [path_key("Work")] + [path_key(f"Work/f{i}.txt") for i in range(200)]
    try:
        build_plan([d], base_count=len(base_keys), p=Profile(), base_keys=base_keys)
    except BulkDeleteAbort as exc:
        assert "201" in str(exc), f"실제 삭제 대상 수가 메시지에 없다: {exc}"
        return
    raise AssertionError("폴더 통삭제가 대량삭제 게이트를 그대로 통과했다")


def test_local_trash_refuses_file_modified_after_scan(tmp_path: Path):
    """삭제는 계획의 맨 마지막에 실행된다 — 그 사이 저장한 편집을 지우면 안 된다."""
    root, store, p = _setup(tmp_path)
    entry = _write(root, "a.txt", b"OLD")
    _write(root, "a.txt", b"USER-EDIT-AFTER-SCAN")   # 스캔 이후 수정

    from dooray_sync.core.differ import Decision
    d = Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="a.txt", key=path_key("a.txt"),
                 local=entry, base=B("a.txt"))
    ex = SyncExecutor(_FakeDrive(), store, p, {}, SyncJournal(store), root_id="root",
                      local_snapshot={path_key("a.txt"): entry})
    rep = ex.run(build_plan([d], p=Profile(name="t", drive_id="d", local_root=str(root),
                                           bulk_delete_abort_count=0,
                                           bulk_delete_abort_ratio=0)))
    assert _read(root, "a.txt") == b"USER-EDIT-AFTER-SCAN", "스캔 이후 편집본을 지웠다"
    assert len(rep.protected) == 1 and not rep.failures
    store.close()


def test_local_trash_refuses_folder_with_unscanned_file(tmp_path: Path):
    """폴더 삭제는 하위에 재귀 적용된다 — 스캔에 없던 파일(제외 대상 포함)이 있으면 멈춘다."""
    root, store, p = _setup(tmp_path)
    known = _write(root, "d/known.txt", b"1")
    _write(root, "d/~$excluded.docx", b"2")          # exclude라 스냅샷에 없다

    from dooray_sync.core.differ import Decision
    dec = Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="d", key=path_key("d"),
                   is_dir=True, base=B("d", is_dir=True))
    ex = SyncExecutor(_FakeDrive(), store, p, {}, SyncJournal(store), root_id="root",
                      local_snapshot={path_key("d/known.txt"): known})
    rep = ex.run(build_plan([dec], p=Profile(name="t", drive_id="d", local_root=str(root),
                                             bulk_delete_abort_count=0,
                                             bulk_delete_abort_ratio=0)))
    assert _read(root, "d/~$excluded.docx") == b"2", "동기화 대상이 아닌 파일을 지웠다"
    assert len(rep.protected) == 1
    store.close()


def test_remote_move_with_content_change_also_downloads():
    """이름 바꾸고 바로 편집하는 것은 흔하다 — 옮기기만 하면 그 편집이 영영 안 온다."""
    old, new = path_key("a.txt"), path_key("sub/a.txt")
    base = {old: B("a.txt", md5="aa", ver=1)}
    view = RemoteView(is_complete=False,
                      entries={new: R("sub/a.txt", "bb", ver=5, size=20)})
    d, _ = diff(base=base, local={old: L("a.txt", "aa")}, remote=view)
    got = [(x.case, x.kind) for x in d]
    assert (12, KIND_LOCAL_MOVE) in got
    assert (13, KIND_DOWNLOAD_UPDATE) in got, f"이동만 하고 내용 수정을 삼켰다: {got}"


def test_remote_move_without_local_original_forgets_old_record():
    """옛 키 레코드를 남기면 file_id가 같은 행이 둘이 되어 이후 편집이 절대 안 올라간다."""
    old, new = path_key("a.txt"), path_key("b.txt")
    d, _ = diff(base={old: B("a.txt")}, local={},
                remote=RemoteView(is_complete=False, entries={new: R("b.txt", "aa")}))
    kinds_got = [(x.kind, x.key) for x in d]
    assert (KIND_DOWNLOAD_NEW, new) in kinds_got
    assert (KIND_FORGET, old) in kinds_got, f"옛 경로 레코드가 정리되지 않는다: {kinds_got}"


def test_delta_truncation_does_not_skip_the_boundary_change():
    """상한에서 끊을 때 흡수하지 않은 항목의 커서로 전진하면 그 1건이 영구 누락된다."""
    client = _FakeApiClient()
    for i in range(5):
        client.add(f"F{i}", f"f{i}.txt", "root", md5="aa")
        client.push_change(f"F{i}", "/", revision=i + 1)
    view = RemoteCollector(DriveAPI(client), "d", "", "root").delta(Cursor(), max_items=2)
    assert view.truncated
    assert len(view.entries) == 2
    assert view.cursor.revision == 2, (
        f"흡수한 것은 2건인데 커서가 revision={view.cursor.revision}까지 갔다 — 그 사이가 누락된다")


def test_iter_incomplete_merges_begin_detail_so_recovery_can_clean_tmp(tmp_path: Path):
    """journal_phase는 begin의 detail을 물려주지 않는다 — 마지막 행만 보면 임시파일 경로가 사라진다."""
    root, store, _p = _setup(tmp_path)
    tmp_dir = root / ".dooraysync_tmp"
    os.makedirs(ext_path(tmp_dir), exist_ok=True)
    tmp_file = tmp_dir / "abc.part"
    with open(ext_path(tmp_file), "wb") as f:
        f.write(b"partial")

    journal = SyncJournal(store)
    jid = journal.begin("DOWNLOAD_NEW", "a.txt", detail={"tmp": str(tmp_file)})
    journal.phase(jid, "started")

    items = list(store.iter_incomplete())
    assert items and items[0]["detail"].get("tmp") == str(tmp_file), "begin detail이 유실됐다"

    rep = recover(store, "d", root)
    assert rep.tmp_removed == 1, "복구가 임시파일을 정리하지 못했다"
    assert not os.path.exists(ext_path(tmp_file))
    store.close()


def test_relist_marks_nested_folders_as_covered():
    """상위와 하위 폴더가 함께 changes에 오면 같은 하위를 깊이만큼 반복 열람한다."""
    client = _FakeApiClient()
    client.add("A", "A", "root", type_="folder")
    client.add("B", "B", "A", type_="folder")
    client.add("C", "C", "B", type_="folder")
    client.add("F", "f.txt", "C", md5="aa")
    for i, fid in enumerate(("A", "B", "C")):
        client.push_change(fid, "/", revision=i + 1)

    view = RemoteCollector(DriveAPI(client), "d", "", "root").delta(Cursor())
    assert view.subtrees_relisted == 1, (
        f"중첩 폴더를 {view.subtrees_relisted}번 재열람했다 — 델타가 전체 순회보다 비싸진다")


def test_local_scan_gap_never_becomes_a_delete():
    """스캔이 못 읽은 경로를 '없음'으로 읽으면 일시적 IO 오류가 원격 삭제로 전파된다."""
    k = path_key("Work/2024/a.txt")
    base = {k: B("Work/2024/a.txt")}
    view = RemoteView(is_complete=True, entries={k: R("Work/2024/a.txt", "aa")})

    # 스캔이 Work/2024 디렉터리 나열에 실패한 상황
    d, stats = diff(base=base, local={}, remote=view, propagate_deletes=True,
                    local_unobserved=["Work/2024"])
    assert [x.kind for x in d] == [KIND_REPORT], f"삭제 판정이 나왔다: {[x.kind for x in d]}"
    assert stats.skipped_local_unobserved == 1

    # 스캔이 정상이면 같은 입력이 삭제로 판정된다(대조군)
    d2, _ = diff(base=base, local={}, remote=view, propagate_deletes=True)
    assert [x.kind for x in d2] == [KIND_REMOTE_TRASH]


def test_folder_trash_deferred_when_live_work_remains_beneath():
    """형제 폴더 삭제 때문에 '보존 승리'(로컬 수정본 재업로드)가 휩쓸려서는 안 된다."""
    from dooray_sync.core.differ import Decision
    ds = [
        Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="Work", key=path_key("Work"),
                 is_dir=True),
        Decision(case=10, kind=KIND_UPLOAD_NEW, rel_path="Work/a.docx",
                 key=path_key("Work/a.docx"), local=L("Work/a.docx", "aa")),
    ]
    pl = build_plan(ds, base_count=100, p=Profile(bulk_delete_abort_count=0,
                                                  bulk_delete_abort_ratio=0))
    assert [a.kind for a in pl.actions] == [KIND_UPLOAD_NEW]
    assert pl.deferred, "폴더 삭제를 미루지 않았다"


def test_child_moves_deferred_under_moved_folder():
    """폴더가 통째로 옮겨지면 자손 개별 이동은 원본을 못 찾아 전부 실패한다."""
    from dooray_sync.core.differ import Decision
    ds = [
        Decision(case=12, kind=KIND_LOCAL_MOVE, rel_path="old", key=path_key("old"),
                 is_dir=True, new_rel_path="new"),
        Decision(case=12, kind=KIND_LOCAL_MOVE, rel_path="old/a.txt",
                 key=path_key("old/a.txt"), new_rel_path="new/a.txt"),
    ]
    pl = build_plan(ds)
    assert [a.rel_path for a in pl.actions] == ["old"]
    assert len(pl.deferred) == 1


def test_dirty_probe_order_rotates_by_last_checked(tmp_path: Path):
    """고정 순서면 예산 안쪽 앞부분만 매번 다시 확인하고 뒤쪽은 영원히 차례가 안 온다."""
    _root, store, _p = _setup(tmp_path)
    for i in range(5):
        store.upsert_file(FileRecord(drive_id="d", rel_path=f"f{i}.txt", file_id=f"F{i}",
                                     sync_status="pending_download"))
    first = store.dirty_file_ids("d")[:2]
    store.touch_seen("d", first)
    second = store.dirty_file_ids("d")[:2]
    assert set(first) & set(second) == set(), (
        f"확인 후에도 같은 것이 다시 앞에 온다: {first} → {second}")
    store.close()


# =========================================================== 실제 휴지통 경로
# 이 절만 send2trash를 **실제로 호출**한다. 나머지 테스트는 삭제 직전 보호에 걸려
# 성공 경로를 지나가지 않으므로, 여기가 없으면 삭제 기능은 한 번도 실행되지 않은 채 남는다.
# 대상은 pytest 임시 디렉터리의 파일이고 휴지통으로 가므로 복구 가능하다.
def _trash_ready() -> bool:
    from dooray_sync.util import trash
    return trash.trash_available()


def test_send_to_trash_actually_removes_file(tmp_path: Path):
    """휴지통 이동이 실제로 동작하는지 — 이 경로가 안 돌면 삭제 기능 전체가 미검증이다."""
    if not _trash_ready():
        print("SKIP: send2trash 없음")
        return
    from dooray_sync.util.trash import send_to_trash

    target = tmp_path / "dsync_test_휴지통.txt"
    with open(ext_path(target), "wb") as f:
        f.write(b"trash me")
    assert os.path.exists(ext_path(target))

    send_to_trash(target)
    assert not os.path.exists(ext_path(target)), "휴지통으로 보내지지 않았다"
    send_to_trash(target)          # 멱등 — 이미 없으면 조용히 반환


def test_send_to_trash_never_falls_back_to_permanent_delete(tmp_path: Path):
    """I5: send2trash를 쓸 수 없으면 **지우지 않고 예외를 올린다.** os.remove 폴백 금지."""
    from dooray_sync.util import trash

    target = tmp_path / "dsync_test_보존.txt"
    with open(ext_path(target), "wb") as f:
        f.write(b"keep me")

    real = trash._import_send2trash
    trash._import_send2trash = lambda: (_ for _ in ()).throw(ImportError("없음"))
    try:
        raised = False
        try:
            trash.send_to_trash(target)
        except trash.TrashUnavailable:
            raised = True
        assert raised, "쓸 수 없는데 예외를 올리지 않았다"
    finally:
        trash._import_send2trash = real
    assert _read(tmp_path, "dsync_test_보존.txt") == b"keep me", "폴백으로 파일을 지웠다"


def test_local_trash_executes_and_clears_record(tmp_path: Path):
    """결정표 8 실행 경로 전체: 원격 삭제 → 로컬 휴지통 → DB 레코드 정리."""
    if not _trash_ready():
        print("SKIP: send2trash 없음")
        return
    root, store, _p = _setup(tmp_path)
    p = Profile(name="t", drive_id="d", local_root=str(root),
                bulk_delete_abort_count=0, bulk_delete_abort_ratio=0)
    entry = _write(root, "dsync_test_삭제대상.txt", b"bye")
    key = path_key("dsync_test_삭제대상.txt")
    store.upsert_file(FileRecord(drive_id="d", rel_path="dsync_test_삭제대상.txt",
                                 file_id="F1", local_md5=_md5(b"bye"),
                                 local_mtime_ns=entry.mtime_ns, local_size=entry.size))

    base = {key: store.get_by_path("d", "dsync_test_삭제대상.txt")}
    view = RemoteView(is_complete=True, deleted_keys={key})
    decisions, _ = diff(base=base, local={key: entry}, remote=view, propagate_deletes=True)
    assert [d.kind for d in decisions] == [KIND_LOCAL_TRASH]

    ex = SyncExecutor(_FakeDrive(), store, p, dict(base), SyncJournal(store),
                      root_id="root", local_snapshot={key: entry})
    rep = ex.run(build_plan(decisions, base_count=1, p=p, base_keys=[key]))

    assert not rep.failures and not rep.protected, (rep.failures, rep.protected)
    assert rep.done.get(KIND_LOCAL_TRASH) == 1
    assert not os.path.exists(ext_path(root / "dsync_test_삭제대상.txt")), "파일이 남아 있다"
    assert store.get_by_path("d", "dsync_test_삭제대상.txt") is None, "DB 레코드가 남았다"
    store.close()


def test_remote_trash_executes_and_clears_record(tmp_path: Path):
    """결정표 7 실행 경로: 로컬 삭제 → 원격 휴지통 → DB 레코드 정리."""
    root, store, _p = _setup(tmp_path)
    p = Profile(name="t", drive_id="d", local_root=str(root),
                bulk_delete_abort_count=0, bulk_delete_abort_ratio=0)
    drive = _FakeDrive()
    fid = drive.put("a.txt", "root", b"x")
    key = path_key("a.txt")
    # remote_* 를 채워야 '원격 불변'이 된다. 비워 두면 differ가 '원격이 바뀌었다'로 보고
    # 결정표 9(보존 승리)를 내는 것이 맞다 — 삭제가 아니라 되받기가 나온다.
    store.upsert_file(FileRecord(drive_id="d", rel_path="a.txt", file_id=fid,
                                 local_md5="aa", local_mtime_ns=1, local_size=1,
                                 remote_version=1, remote_md5="aa", remote_size=1))
    base = {key: store.get_by_path("d", "a.txt")}

    view = RemoteView(is_complete=True,
                      entries={key: R("a.txt", "aa", ver=1, size=1, fid=fid)})
    decisions, _ = diff(base=base, local={}, remote=view, propagate_deletes=True)
    assert [d.kind for d in decisions] == [KIND_REMOTE_TRASH], [d.kind for d in decisions]

    ex = SyncExecutor(drive, store, p, dict(base), SyncJournal(store), root_id="root")
    rep = ex.run(build_plan(decisions, base_count=1, p=p, base_keys=[key]))

    assert not rep.failures
    assert fid in drive.trashed, "원격이 휴지통으로 가지 않았다"
    assert store.get_by_path("d", "a.txt") is None
    store.close()


def test_sync_twice_is_stable(tmp_path: Path):
    """**2패스 검증.** 상태를 남기는 도구의 결함은 대부분 두 번째 실행에서 드러난다
    (실제로 이번에 critical 결함이 정확히 여기서 나왔다)."""
    import contextlib

    from dooray_sync import config as cfg
    from dooray_sync.cli import main as cli

    os.environ[cfg.ENV_CONFIG_DIR] = str(tmp_path / "cfg")
    os.environ[cfg.ENV_STATE_DIR] = str(tmp_path / "state")
    os.environ["DOORAY_API_TOKEN"] = "테스트토큰" + "x" * 30
    root = tmp_path / "local"
    os.makedirs(ext_path(root), exist_ok=True)

    drive = _FakeDrive()
    drive.put("원격.txt", "root", b"R")
    _write(root, "로컬.txt", b"L")

    @contextlib.contextmanager
    def fake_api(p, log):
        yield drive

    real_api = cli._drive_api
    cli._drive_api = fake_api
    try:
        cfg.save_config(cfg.Profile(name="tw", drive_id="d", local_root=str(root)))
        import typer as _typer
        codes = []
        for _ in range(2):
            try:
                cli.sync(profile="tw", dry_run=False, full=True, propagate_deletes=False,
                         allow_bulk_delete=False, md5_probes=200, verbose=False)
                codes.append(0)
            except (SystemExit, _typer.Exit) as exc:
                # typer.Exit는 SystemExit가 아니다(click RuntimeError 계열)
                codes.append(getattr(exc, "exit_code", None) or getattr(exc, "code", 0) or 0)
        assert codes == [0, 0], f"연속 실행이 실패했다: {codes}"
    finally:
        cli._drive_api = real_api
        for k in (cfg.ENV_CONFIG_DIR, cfg.ENV_STATE_DIR, "DOORAY_API_TOKEN"):
            os.environ.pop(k, None)

    # 2패스 뒤에도 기준선이 온전해야 한다 — 여기가 비면 그 파일은 영원히 보류된다
    from dooray_sync.store.db import Store as _S
    with _S(tmp_path / "state" / "tw" / "state.db") as st:
        for rel in ("로컬.txt", "원격.txt"):
            rec = st.get_by_path("d", rel)
            assert rec is not None and rec.local_md5, f"{rel}: 기준선이 비어 있다"
            assert rec.sync_status == "synced", f"{rel}: {rec.sync_status}"
    # 두 번째 실행이 재전송하지 않았다(내용 같으면 version 그대로)
    up = drive.find_child_by_name("d", "root", "로컬.txt")
    assert drive.nodes[up.id]["version"] == 1, "2회차에 같은 내용을 다시 올렸다"


# =========================================================== 2차 적대 검증 회귀
def test_local_move_does_not_commit_unreceived_remote_version(tmp_path: Path):
    """이동은 파일을 옮길 뿐 내용을 받지 않는다. 새 version을 먼저 확정하면
    뒤따르는 DOWNLOAD_UPDATE가 실패했을 때 그 수정이 --full로도 복구되지 않는다."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("sub/a.txt", "root", b"REMOTE-EDITED")   # 원격은 이동 + 수정된 상태
    entry = _write(root, "a.txt", b"OLD")
    old, new = path_key("a.txt"), path_key("sub/a.txt")

    rec = B("a.txt", md5=_md5(b"OLD"), ver=1, size=3, mtime=entry.mtime_ns, fid=fid)
    rec.local_size = entry.size
    store.upsert_file(rec)
    base = {old: store.get_by_path("d", "a.txt")}

    r = R("sub/a.txt", _md5(b"REMOTE-EDITED"), ver=5, size=13, fid=fid)
    view = RemoteView(is_complete=False, entries={new: r})
    decisions, _ = diff(base=base, local={old: entry}, remote=view)
    assert (13, KIND_DOWNLOAD_UPDATE) in [(d.case, d.kind) for d in decisions]

    # 이동만 실행되고 다운로드는 실패하는 상황
    drive.fail_download_once = True
    ex = SyncExecutor(drive, store, p, dict(base), SyncJournal(store), root_id="root")
    ex.run(build_plan(decisions, p=p))

    moved = store.get_by_path("d", "sub/a.txt")
    assert moved is not None
    assert moved.remote_version != 5, (
        "받지도 않은 원격 version을 확정했다 — 그 수정은 영원히 도달하지 않는다")

    # 다음 패스가 반드시 다시 받으려 해야 한다
    again, _ = diff(base={new: moved},
                    local={new: _write(root, "sub/a.txt", b"OLD")},
                    remote=RemoteView(is_complete=False, entries={new: r}))
    assert KIND_DOWNLOAD_UPDATE in [d.kind for d in again], (
        f"재시도가 나오지 않는다: {[(d.case, d.kind) for d in again]}")
    store.close()


def test_local_move_of_edited_file_keeps_old_baseline(tmp_path: Path):
    """UT-12(2026-08-04): 편집된 파일을 이동시킨 뒤 base에 '이동 후 실측 stat + 옛 해시'를
    섞어 기록하면 meta 게이트가 '변경 없음'으로 오판해 편집이 영원히 올라가지 않는다.
    기준선 세 값(mtime/size/md5)은 옛 값 그대로 옮겨야 다음 패스 결정표 4가 잡는다."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("sub/a.txt", "root", b"OLD")       # 원격: 자리만 옮김(내용 불변)
    entry = _write(root, "a.txt", b"LOCAL-EDIT")       # 로컬: 편집된 상태

    old, new = path_key("a.txt"), path_key("sub/a.txt")
    rec = B("a.txt", md5=_md5(b"OLD"), ver=1, size=3, mtime=111, fid=fid)
    store.upsert_file(rec)
    base = {old: store.get_by_path("d", "a.txt")}

    scanner = LocalScanner(root, [])
    r = R("sub/a.txt", _md5(b"OLD"), ver=1, size=3, fid=fid)
    decisions, _ = diff(base=base, local={old: entry},
                        remote=RemoteView(is_complete=True, entries={new: r}),
                        hash_local=scanner.fill_md5)
    assert KIND_LOCAL_MOVE in [d.kind for d in decisions], kinds(decisions)

    ex = SyncExecutor(drive, store, p, dict(base), SyncJournal(store), root_id="root")
    ex.run(build_plan(decisions, p=p))

    moved = store.get_by_path("d", "sub/a.txt")
    assert moved is not None
    assert moved.local_md5 == _md5(b"OLD")
    assert moved.local_mtime_ns == 111 and moved.local_size == 3, (
        "이동 후 실측 stat을 기준선에 기록했다 — 편집이 메타 게이트에 걸려 영원히 안 올라간다")

    # 다음 패스: 편집이 결정표 4(UPLOAD_VERSION)로 잡혀야 한다
    fresh = LocalScanner(root, [])
    entries = fresh.scan()
    again, _ = diff(base={new: moved}, local={new: entries[new]},
                    remote=RemoteView(is_complete=True, entries={new: r}),
                    hash_local=fresh.fill_md5)
    assert KIND_UPLOAD_VERSION in [d.kind for d in again], kinds(again)
    store.close()


def test_resolve_keep_local_leads_to_upload_next_sync(tmp_path: Path):
    """'내 편집본을 살린다'를 고른 파일은 다음 sync에서 **반드시 올라가야** 한다.
    기준선을 지우면 영구 PROTECT, 복원본 해시로 덮으면 영구 무동작이 된다."""
    root, store, p = _setup(tmp_path)
    # 충돌 처리 직후 상태: 원래 경로에 원격본, 옆에 내 편집본 사본
    _write(root, "a.txt", b"REMOTE")
    copy_rel = "a (충돌 2026-08-02 1530).txt"
    _write(root, copy_rel, b"MY-EDIT")
    store.upsert_file(FileRecord(drive_id="d", rel_path="a.txt", file_id="F1",
                                 local_md5=_md5(b"REMOTE"), local_mtime_ns=1, local_size=6,
                                 remote_version=2, remote_md5=_md5(b"REMOTE"),
                                 remote_size=6))
    cid = store.add_conflict("a.txt", "both_modified", str(root / copy_rel))

    from dooray_sync.cli.main import _resolve_one
    row = store.get_conflict(cid)
    assert _resolve_one(store, p, row, "local", False, None)
    assert _read(root, "a.txt") == b"MY-EDIT", "사본이 원래 자리로 돌아오지 않았다"

    # 다음 diff가 '로컬 수정'으로 잡아 올려야 한다
    scanner = LocalScanner(root, [])
    entries = scanner.scan()
    base = {path_key("a.txt"): store.get_by_path("d", "a.txt")}
    view = RemoteView(is_complete=True, entries={
        path_key("a.txt"): R("a.txt", _md5(b"REMOTE"), ver=2, size=6)})
    decisions, _ = diff(base=base, local={path_key("a.txt"): entries[path_key("a.txt")]},
                        remote=view, hash_local=scanner.fill_md5)
    kinds_got = [d.kind for d in decisions if d.rel_path == "a.txt"]
    assert KIND_UPLOAD_VERSION in kinds_got, (
        f"고른 편집본이 업로드되지 않는다: {[(d.case, d.kind, d.reason) for d in decisions]}")
    store.close()


def test_remote_move_without_baseline_protects_local():
    """기준선이 없으면 이동이 끼어도 로컬을 덮지 않는다 — 이동 없을 때와 같은 판단이어야."""
    old, new = path_key("a.txt"), path_key("sub/a.txt")
    rec = FileRecord(drive_id="d", rel_path="a.txt", file_id="F1", local_md5=None,
                     remote_version=1)
    d, _ = diff(base={old: rec}, local={old: L("a.txt", "aa")},
                remote=RemoteView(is_complete=False, entries={new: R("sub/a.txt", "bb")}))
    got = [x.kind for x in d]
    assert "PROTECT" in got, f"기준선 없는데 이동으로 덮어썼다: {got}"
    assert KIND_LOCAL_MOVE not in got


def test_trash_guard_fails_closed_when_subtree_unreadable(tmp_path: Path):
    """os.walk는 오류를 조용히 건너뛴다 — 하위를 못 읽으면 지우지 않아야 한다."""
    root, store, _p = _setup(tmp_path)
    p = Profile(name="t", drive_id="d", local_root=str(root),
                bulk_delete_abort_count=0, bulk_delete_abort_ratio=0)
    known = _write(root, "d/a.txt", b"1")

    from dooray_sync.core.differ import Decision
    dec = Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="d", key=path_key("d"),
                   is_dir=True, base=B("d", is_dir=True))
    ex = SyncExecutor(_FakeDrive(), store, p, {}, SyncJournal(store), root_id="root",
                      local_snapshot={path_key("d/a.txt"): known})

    real_walk = os.walk

    def broken_walk(top, **kw):
        onerror = kw.get("onerror")
        if onerror:
            onerror(OSError(5, "접근 거부(시뮬레이션)"))
        return iter(())

    import dooray_sync.core.executor as ex_mod
    ex_mod.os.walk = broken_walk
    try:
        rep = ex.run(build_plan([dec], p=p))
    finally:
        ex_mod.os.walk = real_walk

    assert _read(root, "d/a.txt") == b"1", "하위를 못 읽었는데 폴더를 지웠다"
    assert len(rep.protected) == 1
    store.close()


def test_trash_refuses_path_the_scan_could_not_read(tmp_path: Path):
    """스캔이 확인 못한 경로는 실행 단계에서도 삭제 대상이 아니다(I1의 실행판)."""
    root, store, _p = _setup(tmp_path)
    p = Profile(name="t", drive_id="d", local_root=str(root),
                bulk_delete_abort_count=0, bulk_delete_abort_ratio=0)
    entry = _write(root, "a.txt", b"1")

    from dooray_sync.core.differ import Decision
    dec = Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path="a.txt", key=path_key("a.txt"),
                   local=entry, base=B("a.txt"))
    ex = SyncExecutor(_FakeDrive(), store, p, {}, SyncJournal(store), root_id="root",
                      local_snapshot={path_key("a.txt"): entry},
                      local_unobserved=["a.txt"])
    rep = ex.run(build_plan([dec], p=p))
    assert _read(root, "a.txt") == b"1"
    assert len(rep.protected) == 1
    store.close()


def test_conflict_copy_gets_a_baseline_even_when_sizes_differ(tmp_path: Path):
    """크기가 다르면 differ가 MD5를 계산하지 않는다 — 그래도 사본은 기준선을 가져야 한다."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("a.txt", "root", b"REMOTE-MUCH-BIGGER")
    entry = _write(root, "a.txt", b"MINE")
    entry.md5 = None                                   # 스캔 직후 상태
    k = path_key("a.txt")

    view = RemoteView(is_complete=True, entries={
        k: R("a.txt", _md5(b"REMOTE-MUCH-BIGGER"), ver=1, size=18, fid=fid)})
    decisions, _ = diff(base={}, local={k: entry}, remote=view)
    assert [d.kind for d in decisions] == [KIND_CONFLICT]
    assert decisions[0].local.md5 is None, "이 테스트의 전제(해시 미계산)가 깨졌다"

    ex = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root",
                      now=lambda: _dt.datetime(2026, 8, 2, 15, 30))
    rep = ex.run(build_plan(decisions, p=p))
    assert not rep.failures, rep.failures

    copy_rel = conflict_copy_name("a.txt", _dt.datetime(2026, 8, 2, 15, 30))
    crec = store.get_by_path("d", copy_rel)
    assert crec is not None
    assert crec.local_md5 or crec.sync_status != "synced", (
        "충돌 사본이 기준선 없는 synced로 기록됐다 — 이후 영구 보류된다")
    store.close()


def test_failed_download_new_stays_retryable_in_delta_mode(tmp_path: Path):
    """신규 수신이 실패하면 file_id가 남아야 델타 모드가 다시 확인한다(I6)."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("new.txt", "root", b"CONTENT")
    k = path_key("new.txt")
    view = RemoteView(is_complete=True, entries={
        k: R("new.txt", _md5(b"CONTENT"), size=7, fid=fid)})
    decisions, _ = diff(base={}, local={}, remote=view)

    drive.fail_download_once = True
    ex = SyncExecutor(drive, store, p, {}, SyncJournal(store), root_id="root")
    rep = ex.run(build_plan(decisions, p=p))
    assert len(rep.failures) == 1

    assert store.dirty_file_ids("d") == [fid], (
        "실패한 신규 수신이 dirty 목록에서 빠져 델타 모드가 영원히 재시도하지 않는다")
    store.close()


def test_locally_deleted_folder_is_not_recreated_every_run():
    """사용자가 지운 폴더가 매 실행 되살아나면 안 된다. 단 init 직후(미실현)는 만들어야 한다."""
    k = path_key("폴더")
    r = R("폴더", is_dir=True, fid="D1")
    view = RemoteView(is_complete=True, entries={k: r})

    # (a) 한 번도 로컬에 만든 적 없음(init 직후) → 만든다
    fresh = FileRecord(drive_id="d", rel_path="폴더", file_id="D1", is_dir=True,
                       sync_status="pending_download")
    d, _ = diff(base={k: fresh}, local={}, remote=view)
    assert [x.kind for x in d] == [KIND_MKDIR_LOCAL]

    # (b) 만들었었는데 사용자가 지움 → 되살리지 않는다
    synced = FileRecord(drive_id="d", rel_path="폴더", file_id="D1", is_dir=True,
                        sync_status="synced")
    d2, _ = diff(base={k: synced}, local={}, remote=view)
    assert KIND_MKDIR_LOCAL not in [x.kind for x in d2], "지운 폴더를 다시 만들었다"
    assert [x.kind for x in d2] == [KIND_REPORT]

    # (c) 삭제 전파를 켜면 원격 폴더를 휴지통으로 (폴더판 결정표 7이 살아 있는지)
    d3, _ = diff(base={k: synced}, local={}, remote=view, propagate_deletes=True)
    assert [x.kind for x in d3] == [KIND_REMOTE_TRASH]


def test_delta_keeps_seeing_changes_after_cursor_saved():
    """커서를 저장한 뒤에도 그다음 원격 변경을 계속 봐야 한다.

    실계정에서 드러난 결함: 커서에 fileId가 실려 나가면 그 시점 이후 changes가
    영구히 0건이 되어 원격 변경을 통째로 놓쳤다. 페이크 클라이언트는 fileId가
    오면 서버처럼 결과를 걸러 그 상황을 재현한다.
    """
    class _FilteringClient(_FakeApiClient):
        """fileId가 오면 그 파일 것만 돌려주는 서버(실계정 관측과 같은 방향의 왜곡)."""

        def api(self, method, path, **kw):
            params = kw.get("params") or {}
            if "/changes" in path and params.get("fileId"):
                after = int(params.get("latestRevision") or 0)
                fid = str(params["fileId"])
                rest = [c for c in self.changes
                        if int(c["revision"]) > after and c["file"]["id"] == fid]
                return {"header": {"isSuccessful": True}, "result": rest}
            return super().api(method, path, **kw)

    client = _FilteringClient()
    client.add("F1", "a.txt", "root", md5="aa")
    client.push_change("F1", "/", revision=10)
    collector = RemoteCollector(DriveAPI(client), "d", "", "root")

    first = collector.delta(Cursor(revision=0))
    assert first.changes_seen == 1
    saved = first.cursor
    assert saved.revision == 10

    # 그 뒤 다른 파일이 바뀐다
    client.add("F2", "b.txt", "root", md5="bb")
    client.push_change("F2", "/", revision=11)

    second = collector.delta(saved)
    assert second.changes_seen == 1, (
        "저장된 커서로 재질의했더니 새 변경을 못 봤다 — 델타가 영구히 멀어진다")
    assert path_key("b.txt") in second.entries


def test_delta_truncation_backs_off_to_fully_consumed_revision():
    """같은 revision에 항목이 여럿일 때 그 중간에서 끊고 revision을 물리면 형제가 누락된다."""
    client = _FakeApiClient()
    for i in range(3):
        client.add(f"S{i}", f"s{i}.txt", "root", md5="aa")
        client.push_change(f"S{i}", "/", revision=7)      # 셋 다 같은 revision
    client.add("T", "t.txt", "root", md5="bb")
    client.push_change("T", "/", revision=5)              # 앞선 revision

    # 정렬: revision 5 → 7,7,7. max_items=2면 revision 7 한가운데서 끊긴다.
    client.changes.sort(key=lambda c: int(c["revision"]))
    view = RemoteCollector(DriveAPI(client), "d", "", "root").delta(Cursor(), max_items=2)
    assert view.truncated
    assert view.cursor.revision == 5, (
        f"revision {view.cursor.revision}로 물렸다 — 같은 revision의 나머지가 누락된다")


def test_init_then_sync_downloads_instead_of_deleting():
    """**실계정에서 잡힌 결함.** init은 원격 상태만 기록하고 파일은 받지 않는다.
    그 상태의 '로컬에 없음'을 삭제로 읽으면, 새 PC의 첫 sync가 삭제 전파가 켜져 있을 때
    원격 파일을 전부 휴지통으로 보낸다."""
    k = path_key("a.txt")
    # init이 만든 레코드: 원격 메타는 있고 로컬 기준선은 없다
    rec = FileRecord(drive_id="d", rel_path="a.txt", file_id="F1",
                     remote_version=1, remote_size=10, remote_md5="aa",
                     local_md5=None, sync_status="pending_download")
    view = RemoteView(is_complete=True, entries={k: R("a.txt", "aa", ver=1, size=10)})

    d, _ = diff(base={k: rec}, local={}, remote=view)
    assert [(x.case, x.kind) for x in d] == [(2, KIND_DOWNLOAD_NEW)], (
        f"받아야 할 파일을 다른 것으로 판정했다: {[(x.case, x.kind, x.reason) for x in d]}")

    # 삭제 전파를 켜도 마찬가지다 — 여기서 REMOTE_TRASH가 나오면 새 PC가 원격을 지운다
    d2, _ = diff(base={k: rec}, local={}, remote=view, propagate_deletes=True)
    assert KIND_REMOTE_TRASH not in [x.kind for x in d2], (
        "init 직후 첫 sync가 원격 파일을 삭제하려 한다 — 새 PC 설치 시 전체 소실")

    # 대조군: 한 번 받았던 파일(기준선 있음)이 사라지면 그건 진짜 로컬 삭제다
    had = B("a.txt", md5="aa", ver=1, size=10)
    d3, _ = diff(base={k: had}, local={}, remote=view, propagate_deletes=True)
    assert [(x.case, x.kind) for x in d3] == [(7, KIND_REMOTE_TRASH)]


def _run_all() -> int:
    import inspect
    import tempfile
    import traceback

    fails = 0
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for name, fn in fns:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} 통과")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(_run_all())
