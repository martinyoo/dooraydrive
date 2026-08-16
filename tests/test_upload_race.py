"""두 PC 업로드 경합 방어(M3 단위 6, 설계 §6.3).

계획 수립(원격 관측)과 업로드 실행 사이에 다른 PC가 새 버전을 올렸으면,
그 내용을 조용히 덮지 않고 보호로 물러나야 한다 — 다음 패스의 differ가
'양쪽 변경'으로 판정해 충돌 보존한다.
"""
from __future__ import annotations

import os
from pathlib import Path

# test_m2의 페이크 원격·헬퍼를 재사용한다(같은 서버 의미론을 두 벌 만들지 않는다).
from tests.test_m2 import _FakeDrive, _md5, _setup, _write

from dooray_sync.core.differ import KIND_UPLOAD_VERSION, Decision
from dooray_sync.core.journal import SyncJournal
from dooray_sync.core.executor import SyncExecutor
from dooray_sync.core.planner import plan as build_plan
from dooray_sync.store.db import FileRecord
from dooray_sync.util.paths import path_key


def _race_setup(tmp_path: Path):
    """base는 v1을 알고, 원격은 다른 PC의 업로드로 v2가 된 상태."""
    root, store, p = _setup(tmp_path)
    drive = _FakeDrive()
    fid = drive.put("a.txt", "root", b"PC-B-NEWER")
    drive.nodes[fid]["version"] = 2                    # 다른 PC가 이미 v2를 올림

    entry = _write(root, "a.txt", b"PC-A-EDIT")
    rec = FileRecord(drive_id="d", rel_path="a.txt", file_id=fid,
                     remote_version=1, local_md5="stale", sync_status="synced")
    base = {path_key("a.txt"): rec}
    d = Decision(case=4, kind=KIND_UPLOAD_VERSION, rel_path="a.txt",
                 key=path_key("a.txt"), local=entry, base=rec)
    return root, store, p, drive, fid, base, d


def test_upload_version_yields_when_remote_is_ahead(tmp_path: Path):
    root, store, p, drive, fid, base, d = _race_setup(tmp_path)
    ex = SyncExecutor(drive, store, p, base, SyncJournal(store), root_id="root")
    rep = ex.run(build_plan([d], p=p))

    assert not rep.failures
    assert len(rep.protected) == 1
    assert "버전" in rep.protected[0][1]
    assert drive.nodes[fid]["content"] == b"PC-B-NEWER", "다른 PC의 최신본을 덮어썼다"
    assert drive.nodes[fid]["version"] == 2, "업로드가 실행돼 버전이 올랐다"
    # 다음 실행이 다시 보도록 표시된다(성공으로 굳히지 않는다)
    after = store.get_by_path("d", "a.txt")
    assert after is not None and after.sync_status == "pending_upload"
    store.close()


def test_upload_version_proceeds_when_version_matches(tmp_path: Path):
    """기준선과 원격 버전이 같으면(경합 없음) 정상 업로드 — 가드의 오탐 방지."""
    root, store, p, drive, fid, base, d = _race_setup(tmp_path)
    drive.nodes[fid]["version"] = 1                    # 경합 없음: 원격이 아는 그대로
    ex = SyncExecutor(drive, store, p, base, SyncJournal(store), root_id="root")
    rep = ex.run(build_plan([d], p=p))

    assert not rep.failures and not rep.protected
    assert drive.nodes[fid]["content"] == b"PC-A-EDIT"
    assert drive.nodes[fid]["version"] == 2            # 정상적으로 한 단계 상승
    store.close()
