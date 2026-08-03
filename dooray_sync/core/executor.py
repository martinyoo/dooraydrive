"""계획 실행 — 규약_M2 §6.

여기가 사용자 데이터에 실제로 손을 대는 유일한 곳이다. 지켜야 할 것:

- **C1** 원자적 다운로드(임시파일 → 검증 → os.replace), **C2** os.replace 직전 재-stat
- **C3** 모든 로컬 IO는 `ext_path` 경유
- **C4** 업로드·폴더생성 응답의 **서버 저장명이 정본**(R14)
- **D1** 업로드 분기는 부모 폴더 색인으로. 409는 재조회 후 재판정
- **I4** base 갱신은 저널 'committed'와 한 트랜잭션
- **I5** 사용자 파일 삭제는 휴지통으로만

실패는 **항목 단위로 격리**한다. 하나가 실패해도 나머지는 계속 처리하고, 실패한 항목은
DB에 error로 남겨 다음 실행이 반드시 다시 본다.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..api.client import DoorayApiError
from ..api.drive import NO_ACCESS_AUTHORITY, DriveAPI
from ..api.models import RemoteFile
from ..config import Profile
from ..store.db import FileRecord, Store, now_iso
from ..util.hashing import md5_file
from ..util.paths import (
    ext_path,
    local_path,
    path_key,
    rel_posix,
    server_name_will_differ,
    to_nfc,
)
from ..util.trash import TrashUnavailable, send_to_trash
from .differ import (
    _remote_changed,
    KIND_CONFLICT,
    KIND_DOWNLOAD_NEW,
    KIND_DOWNLOAD_UPDATE,
    KIND_FORGET,
    KIND_LOCAL_MOVE,
    KIND_LOCAL_TRASH,
    KIND_MKDIR_LOCAL,
    KIND_MKDIR_REMOTE,
    KIND_REMOTE_MOVE,
    KIND_REMOTE_TRASH,
    KIND_TOUCH_BASE,
    KIND_UPLOAD_NEW,
    KIND_UPLOAD_VERSION,
    conflict_copy_name,
)
from .journal import SyncJournal
from .planner import Plan

__all__ = ["SyncExecutor", "ExecReport", "LocalChangedDuringSync"]

log = logging.getLogger(__name__)


class LocalChangedDuringSync(RuntimeError):
    """전송 중 로컬이 바뀌어 교체를 포기했다. **실패가 아니라 보호다.**"""


@dataclass
class ExecReport:
    done: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)
    protected: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    renamed_by_server: list[tuple[str, str]] = field(default_factory=list)
    bytes_up: int = 0
    bytes_down: int = 0

    def tick(self, kind: str) -> None:
        self.done[kind] = self.done.get(kind, 0) + 1


def _stat_or_none(path: Path | str) -> os.stat_result | None:
    try:
        return os.stat(ext_path(path))
    except OSError:
        return None


class SyncExecutor:
    """Action 실행기."""

    def __init__(self, drive: DriveAPI, store: Store, p: Profile,
                 base: dict[str, FileRecord], journal: SyncJournal, *,
                 root_id: str, remote_prefix: str = "",
                 logger: logging.Logger | None = None,
                 now: Callable[[], _dt.datetime] | None = None,
                 local_snapshot: dict | None = None,
                 local_unobserved: Sequence[str] = ()) -> None:
        self.drive = drive
        self.store = store
        self.p = p
        self.base = base
        self.journal = journal
        self.root = p.root_path
        self.drive_id = p.drive_id
        self.root_id = root_id
        self.prefix = remote_prefix
        self.log = logger or log
        self._now = now or _dt.datetime.now
        # 스캔 시점 로컬 스냅샷. 폴더 삭제 직전 하위 전체를 대조하는 데 쓴다.
        self.local_snapshot = local_snapshot or {}
        # 스캔이 확인하지 못한 경로(키). 이 아래는 삭제 대상에서 제외한다(I1).
        self.local_unobserved = tuple(
            path_key(u) for u in (local_unobserved or ()) if u and u != "(루트)")
        self.folder_ids: dict[str, str] = {"": root_id}
        self._index: dict[str, dict[str, RemoteFile]] = {}
        self._folded: dict[str, dict[str, RemoteFile]] = {}
        self.report = ExecReport()

    # ------------------------------------------------------------------ 실행 루프
    def run(self, plan: Plan, *, on_progress: Callable[[object], None] | None = None) -> ExecReport:
        handlers = {
            KIND_MKDIR_REMOTE: self.mkdir_remote,
            KIND_MKDIR_LOCAL: self.mkdir_local,
            KIND_UPLOAD_NEW: self.upload,
            KIND_UPLOAD_VERSION: self.upload,
            KIND_DOWNLOAD_NEW: self.download,
            KIND_DOWNLOAD_UPDATE: self.download,
            KIND_CONFLICT: self.conflict,
            KIND_LOCAL_MOVE: self.local_move,
            KIND_REMOTE_MOVE: self.remote_move,
            KIND_LOCAL_TRASH: self.local_trash,
            KIND_REMOTE_TRASH: self.remote_trash,
            KIND_TOUCH_BASE: self.touch_base,
            KIND_FORGET: self.forget,
        }
        for action in plan.actions:
            fn = handlers.get(action.kind)
            if fn is None:
                self.log.warning("알 수 없는 동작 — 건너뜁니다: %s", action.kind)
                continue
            try:
                fn(action)
            except LocalChangedDuringSync as exc:
                # 로컬을 지킨 것이므로 실패로 집계하지 않는다(종료코드에 영향 없음).
                self.report.protected.append((action.rel_path, str(exc)))
                self._mark(action.rel_path, action.is_dir, "pending_download", str(exc))
                self.log.warning("보호: %s", exc)
            except TrashUnavailable as exc:
                self.report.protected.append((action.rel_path, str(exc).splitlines()[0]))
                self.log.warning("삭제 보류: %s", exc)
            except Exception as exc:  # noqa: BLE001 — 항목 단위 격리
                reason = f"{type(exc).__name__}: {exc}"
                self.report.failures.append((action.rel_path, reason))
                self._mark(action.rel_path, action.is_dir, "error", reason, action=action)
                self.log.error("실패 %s (%s): %s", action.rel_path, action.kind, reason)
                self.log.debug("실패 상세 %s", action.rel_path, exc_info=True)
            if on_progress is not None:
                on_progress(action)
        return self.report

    # ------------------------------------------------------------------ 원격 폴더
    def _dir_index(self, parent_id: str) -> dict[str, RemoteFile]:
        idx = self._index.get(parent_id)
        if idx is None:
            idx = {}
            folded: dict[str, RemoteFile] = {}
            for child in self.drive.iter_children(self.drive_id, parent_id):
                if child.sub_type == "trash":
                    continue
                name = to_nfc(child.name)
                idx[name] = child
                folded.setdefault(path_key(name), child)
            self._index[parent_id] = idx
            self._folded[parent_id] = folded
        return idx

    def _idx_find(self, parent_id: str, *names: str) -> RemoteFile | None:
        """정확 일치 우선, 없으면 대소문자 무시(서버의 중복 검사가 그렇다 — 실측)."""
        idx = self._dir_index(parent_id)
        for n in names:
            if n and n in idx:
                return idx[n]
        folded = self._folded.get(parent_id) or {}
        for n in names:
            k = path_key(n) if n else ""
            if k and k in folded:
                return folded[k]
        return None

    def _idx_put(self, parent_id: str, rf: RemoteFile) -> None:
        name = to_nfc(rf.name)
        self._dir_index(parent_id)[name] = rf
        self._folded.setdefault(parent_id, {})[path_key(name)] = rf

    def ensure_remote_folder(self, rel: str) -> str:
        """폴더 상대경로 → 원격 folder id. 상위부터 재귀 확인·생성."""
        rel = str(rel or "").strip("/")
        if not rel:
            return self.root_id
        key = path_key(rel)
        if key in self.folder_ids:
            return self.folder_ids[key]

        parent_rel, _, name = rel.rpartition("/")
        parent_id = self.ensure_remote_folder(parent_rel) if parent_rel else self.root_id
        rec = self.base.get(key)
        lookup = to_nfc(rec.server_name) if (rec and rec.server_name) else ""
        found = self._idx_find(parent_id, lookup, to_nfc(name))

        if found is not None and not found.is_dir:
            raise DoorayApiError(f"원격에 같은 이름의 파일이 있어 폴더를 만들 수 없습니다: {rel}")
        if found is not None:
            fid, server_name = found.id, found.name
        else:
            created, is_new = self.drive.create_folder_ex(self.drive_id, parent_id, name)
            fid = created.id
            server_name = created.name or to_nfc(name)
            self._idx_put(parent_id, created)
            if is_new:
                # 방금 만든 폴더만 '비어 있다'고 단정할 수 있다. 409로 받은 기존 폴더에
                # 이걸 적용하면 하위 항목을 통째로 못 본다.
                self._index[fid] = {}
                self._folded[fid] = {}

        self.folder_ids[key] = fid
        self._save(FileRecord(
            drive_id=self.drive_id, rel_path=rel, file_id=fid, parent_id=parent_id,
            server_name=server_name, is_dir=True, sync_status="synced",
            last_synced_at=now_iso(),
        ))
        return fid

    # ------------------------------------------------------------------ 동작들
    def mkdir_remote(self, action) -> None:
        entry = self.journal.begin(action.kind, action.rel_path, detail={"is_dir": True})
        self.journal.phase(entry, "started")
        fid = self.ensure_remote_folder(action.rel_path)
        self.journal.commit(entry, lambda: None, {"file_id": fid})
        self.report.tick(action.kind)

    def mkdir_local(self, action) -> None:
        r = action.decision.remote if action.decision else None
        dest = local_path(self.root, action.rel_path)
        entry = self.journal.begin(action.kind, action.rel_path, detail={"is_dir": True})
        self.journal.phase(entry, "started")
        os.makedirs(ext_path(dest), exist_ok=True)
        rec = FileRecord(
            drive_id=self.drive_id, rel_path=action.rel_path,
            file_id=(r.file_id if r else None), parent_id=(r.parent_id if r else None),
            server_name=(r.server_name if r else None), is_dir=True,
            remote_revision=(r.revision if r else None),
            remote_version=(r.version if r else None),
            sync_status="synced", last_synced_at=now_iso(),
        )
        self.journal.commit(entry, lambda: self._save(rec))
        self.report.tick(action.kind)

    def upload(self, action) -> None:
        d = action.decision
        entry_local = d.local if d else None
        if entry_local is None:
            raise RuntimeError("업로드할 로컬 항목 정보가 없습니다")

        rel = action.rel_path
        parent_rel, _, name = rel.rpartition("/")
        parent_id = self.ensure_remote_folder(parent_rel) if parent_rel else self.root_id
        src = Path(entry_local.disk_path) if entry_local.disk_path else local_path(self.root, rel)

        rec = self.base.get(action.key)
        lookup = to_nfc(rec.server_name) if (rec and rec.server_name) else ""
        found = self._idx_find(parent_id, lookup, to_nfc(name))
        if found is not None and found.is_dir:
            raise DoorayApiError(f"원격에 같은 이름의 폴더가 있어 업로드할 수 없습니다: {rel}")

        # 덮어쓰기 안전장치: '신규 업로드'인데 원격에 같은 이름이 이미 있으면,
        # 그것이 우리가 아는 그 파일일 때만 새 버전으로 올린다. 모르는 파일이면
        # **덮어쓰지 않고** 다음 패스에서 내용을 대조하게 넘긴다(남의 최신본 보호).
        known_id = rec.file_id if rec else None
        if action.kind == KIND_UPLOAD_NEW and found is not None and found.id != known_id:
            self.report.protected.append((
                rel, "원격에 같은 이름의 파일이 이미 있습니다 — 덮어쓰지 않았습니다"
                     " (다음 동기화에서 내용을 대조합니다)"))
            self._mark(rel, False, "pending_upload",
                       "원격에 같은 이름이 존재 — 내용 대조 필요")
            return
        if (action.kind == KIND_UPLOAD_VERSION and found is not None
                and known_id and found.id != known_id):
            self.report.protected.append((
                rel, "원격 파일이 우리가 아는 것과 다릅니다(id 불일치) — 덮어쓰지 않았습니다"))
            self._mark(rel, False, "pending_upload", "원격 파일 id 불일치 — 대조 필요")
            return

        jid = self.journal.begin(action.kind, rel, file_id=(found.id if found else known_id),
                                 detail={"size": entry_local.size, "md5": entry_local.md5})
        self.journal.phase(jid, "started")

        if found is not None:
            res = self.drive.upload_version(self.drive_id, found.id, name, src)
            file_id = str(res.get("id") or found.id)
            version = res.get("version")
            server_name = found.name
            did = KIND_UPLOAD_VERSION
        else:
            try:
                rf = self.drive.upload_new(self.drive_id, parent_id, name, src)
            except DoorayApiError as exc:
                if exc.status != 409:
                    raise
                # D1: 409 = 이름 충돌(경쟁 상태 또는 대소문자만 다른 동명). 재조회 후 재판정.
                self.log.warning("409 이름 충돌 → 재조회: %s", rel)
                self._index.pop(parent_id, None)
                self._folded.pop(parent_id, None)
                existing = self.drive.find_child_by_name(self.drive_id, parent_id, name)
                if existing is None or existing.is_dir:
                    raise
                if known_id and existing.id != known_id:
                    self.journal.fail(jid, "원격에 다른 파일이 있어 덮어쓰지 않음")
                    self.report.protected.append((
                        rel, "409 재조회 결과 원격 파일이 우리가 아는 것과 다릅니다 — 덮어쓰지 않았습니다"))
                    self._mark(rel, False, "pending_upload", "409 후 id 불일치 — 대조 필요")
                    return
                res = self.drive.upload_version(self.drive_id, existing.id, name, src)
                rf = RemoteFile(id=str(res.get("id") or existing.id), name=existing.name,
                                type="file", version=res.get("version") or 0)
            file_id = rf.id
            version = rf.version
            server_name = rf.name or to_nfc(name)
            self._idx_put(parent_id, RemoteFile(id=file_id, name=server_name, type="file"))
            did = KIND_UPLOAD_NEW

        self.journal.phase(jid, "transferred", {"file_id": file_id})
        if server_name and path_key(server_name) != path_key(name):
            # R14: 서버가 이름을 바꿨다. 정본은 서버 저장명이다.
            self.report.renamed_by_server.append((name, str(server_name)))

        # 전송 중 로컬이 또 바뀌었으면 다음 실행이 잡도록 표시한다(성공으로 덮지 않는다).
        st = _stat_or_none(src)
        moved = st is not None and (st.st_mtime_ns != entry_local.mtime_ns
                                    or st.st_size != entry_local.size)

        # **기준선 없는 'synced'를 만들지 않는다.** differ는 신규 업로드(결정표 1·10)에서는
        # 비교할 대상이 없어 해시를 계산하지 않으므로 entry.md5가 비어 있을 수 있다.
        # 그대로 local_md5=NULL로 기록하면 다음 실행의 _has_baseline이 실패해 그 파일은
        # 영원히 PROTECT만 된다 — 업로드가 1회용이 되는 가장 조용한 실패다.
        md5 = entry_local.md5
        md5_problem = ""
        if not md5 and not moved:
            try:
                md5 = md5_file(src)
            except OSError as exc:
                md5 = None
                md5_problem = f"업로드 후 해시 계산 실패 — {type(exc).__name__}: {exc}"
                self.log.warning("%s: %s", rel, md5_problem)

        status = "synced"
        note = None
        if moved:
            status, note = "pending_upload", "전송 중 로컬 파일이 변경됨 — 다음 실행에서 재전송"
        elif not md5:
            # 기준선을 확정하지 못했으면 synced라고 하지 않는다. 다음 실행이 다시 본다.
            status, note = "pending_upload", md5_problem or "기준선(해시) 미확정 — 다음 실행에서 재확인"

        rec_new = FileRecord(
            drive_id=self.drive_id, rel_path=rel, file_id=file_id or None,
            parent_id=parent_id or None, server_name=server_name or None, is_dir=False,
            local_mtime_ns=entry_local.mtime_ns, local_size=entry_local.size,
            local_md5=md5,
            remote_version=version if isinstance(version, int) else None,
            remote_md5=md5, remote_size=entry_local.size,
            sync_status=status, error_msg=note,
            last_synced_at=now_iso(),
        )
        self.journal.commit(jid, lambda: self._save(rec_new))
        self.report.bytes_up += int(entry_local.size or 0)
        self.report.tick(did)

    def download(self, action, *, dest_rel: str | None = None,
                 expect_absent: bool = False, count: bool = True) -> None:
        d = action.decision
        r = d.remote if d else None
        if r is None or not r.file_id:
            raise RuntimeError("받을 원격 항목 정보가 없습니다")

        rel = dest_rel or action.rel_path
        dest = local_path(self.root, rel)
        scanned = None if expect_absent else (d.local if d else None)

        # C2 1차: 계획 시점과 지금이 다르면 이 항목은 건너뛴다.
        st = _stat_or_none(dest)
        if scanned is not None:
            if st is None:
                self.log.info("계획 후 로컬 파일이 사라짐 — 신규로 받습니다: %s", rel)
            elif st.st_mtime_ns != scanned.mtime_ns or st.st_size != scanned.size:
                raise LocalChangedDuringSync(f"다운로드 직전 로컬이 변경됨: {rel}")
        elif st is not None and not expect_absent:
            raise LocalChangedDuringSync(f"계획에 없던 로컬 파일이 생겼습니다: {rel}")

        expect = (st.st_mtime_ns, st.st_size) if st is not None else None

        def _guard(_dest: Path = dest, _rel: str = rel, _expect=expect) -> None:
            # C2 본검사: os.replace 직전. 전송에 걸린 시간 동안 사용자가 저장했을 수 있다.
            now = _stat_or_none(_dest)
            if _expect is None:
                if now is not None:
                    raise LocalChangedDuringSync(f"전송 중 로컬에 파일이 생성됨: {_rel}")
                return
            if now is None:
                raise LocalChangedDuringSync(f"전송 중 로컬 파일이 사라짐: {_rel}")
            if (now.st_mtime_ns, now.st_size) != _expect:
                raise LocalChangedDuringSync(f"전송 중 로컬 파일이 수정됨: {_rel}")

        jid = self.journal.begin(action.kind, rel, file_id=r.file_id,
                                 detail={"remote_version": r.version, "size": r.size})
        self.journal.phase(jid, "started")
        os.makedirs(ext_path(dest.parent), exist_ok=True)

        def _note_tmp(tmp: Path, _jid: int = jid) -> None:
            # 복구가 지울 대상을 알 수 있도록 임시파일 경로를 저널에 남긴다.
            self.journal.phase(_jid, "started", {"tmp": str(tmp)})

        info = self.drive.download(self.drive_id, r.file_id, dest,
                                   expected_size=r.size, expected_md5=r.md5,
                                   pre_replace_guard=_guard, on_tmp=_note_tmp)
        self.journal.phase(jid, "verified", {"bytes": info.get("bytes")})

        md5 = str(info.get("md5") or "") or None
        fresh = _stat_or_none(dest)
        rec = FileRecord(
            drive_id=self.drive_id, rel_path=rel, file_id=r.file_id or None,
            parent_id=r.parent_id or None, server_name=r.server_name or None, is_dir=False,
            local_mtime_ns=fresh.st_mtime_ns if fresh else None,
            local_size=fresh.st_size if fresh else info.get("bytes"),
            local_md5=md5,
            remote_revision=r.revision, remote_version=r.version,
            remote_md5=r.md5 or md5, remote_size=r.size,
            sync_status="synced", last_synced_at=now_iso(),
        )
        self.journal.commit(jid, lambda: self._save(rec))
        self.report.bytes_down += int(info.get("bytes") or 0)
        if count:
            # 충돌 처리가 내부적으로 부를 때는 세지 않는다(한 동작이 두 번 집계된다).
            self.report.tick(action.kind)

    def conflict(self, action) -> None:
        """결정표 6/3: 양쪽이 다르게 바뀌었다 — **둘 다 남긴다**(규약_M2 I3).

        순서를 바꾸면 유실 창이 생긴다:
          1) 로컬 원본을 충돌 사본으로 개명   ← 여기까지만 돼도 로컬 내용은 안전하다
          2) conflicts 기록
          3) 원격을 원래 경로로 받기(빈 자리라 덮어쓸 것이 없다)
          4) 사본을 원격에 올리기(옵션)
        """
        d = action.decision
        entry_local = d.local if d else None
        r = d.remote if d else None
        if entry_local is None or r is None:
            raise RuntimeError("충돌 처리에 필요한 정보가 없습니다")

        rel = action.rel_path
        src = Path(entry_local.disk_path) if entry_local.disk_path else local_path(self.root, rel)
        copy_rel = self._free_conflict_rel(rel)
        copy_path = local_path(self.root, copy_rel)

        jid = self.journal.begin(KIND_CONFLICT, rel, file_id=r.file_id,
                                 detail={"conflict_copy": str(copy_path)})
        self.journal.phase(jid, "started")

        # 1) 개명 — 전송 전에 로컬 내용을 안전한 자리로 옮긴다
        st = _stat_or_none(src)
        if st is None:
            raise LocalChangedDuringSync(f"충돌 처리 직전 로컬 파일이 사라짐: {rel}")
        if (st.st_mtime_ns, st.st_size) != (entry_local.mtime_ns, entry_local.size):
            raise LocalChangedDuringSync(f"충돌 처리 직전 로컬이 변경됨: {rel}")
        os.makedirs(ext_path(copy_path.parent), exist_ok=True)
        os.replace(ext_path(src), ext_path(copy_path))
        self.journal.phase(jid, "transferred", {"conflict_copy": str(copy_path)})

        # 2) 기록
        self.store.add_conflict(rel, "both_modified", str(copy_path))
        self.report.conflicts.append((rel, str(copy_path)))

        # 3) 원격 수신 — 원래 경로는 비어 있다
        self.download(action, expect_absent=True, count=False)

        # 4) 사본 업로드(기본 on). 실패해도 로컬 사본은 그대로 남는다.
        copy_stat = _stat_or_none(copy_path)
        # 사본의 해시를 여기서 한 번 확정한다. 결정표 3(크기 상이)에서는 differ가 MD5를
        # 계산하지 않으므로 entry_local.md5가 비어 있을 수 있는데, 그대로 두면 이 사본이
        # 이후 '기준선 없음'으로 영구 PROTECT가 된다.
        copy_md5 = entry_local.md5
        if not copy_md5:
            try:
                copy_md5 = md5_file(copy_path)
            except OSError as exc:
                self.log.warning("충돌 사본 해시 계산 실패 %s: %s", copy_rel, exc)
        copy_rec = FileRecord(
            drive_id=self.drive_id, rel_path=copy_rel, is_dir=False,
            local_mtime_ns=copy_stat.st_mtime_ns if copy_stat else None,
            local_size=copy_stat.st_size if copy_stat else None,
            local_md5=copy_md5,
            sync_status="pending_upload",
            error_msg="충돌 사본 — 원격에 올릴 대상",
        )
        self._save(copy_rec)
        if self.p.upload_conflict_copy:
            try:
                self._upload_conflict_copy(copy_rel, copy_path, copy_md5)
            except Exception as exc:  # noqa: BLE001 — 사본 업로드 실패는 유실이 아니다
                self.log.warning("충돌 사본 업로드 실패(로컬에는 남아 있습니다) %s: %s",
                                 copy_rel, exc)
        self.journal.phase(jid, "committed", {"conflict_copy": str(copy_path)})
        self.report.tick(KIND_CONFLICT)

    def _upload_conflict_copy(self, copy_rel: str, copy_path: Path, md5: str | None) -> None:
        parent_rel, _, name = copy_rel.rpartition("/")
        parent_id = self.ensure_remote_folder(parent_rel) if parent_rel else self.root_id
        rf = self.drive.upload_new(self.drive_id, parent_id, name, copy_path)
        st = _stat_or_none(copy_path)

        # upload()와 같은 규칙: **기준선 없는 'synced'를 만들지 않는다.**
        # 결정표 3에서 크기가 다르면 differ가 MD5를 계산하지 않으므로 md5가 비어 올 수 있다.
        # 그대로 synced로 기록하면 사용자가 살리려고 남긴 충돌 사본이 이후 영구 PROTECT가 된다.
        if not md5:
            try:
                md5 = md5_file(copy_path)
            except OSError as exc:
                self.log.warning("충돌 사본 해시 계산 실패 %s: %s", copy_rel, exc)
                md5 = None

        self._save(FileRecord(
            drive_id=self.drive_id, rel_path=copy_rel, file_id=rf.id or None,
            parent_id=parent_id, server_name=rf.name or to_nfc(name), is_dir=False,
            local_mtime_ns=st.st_mtime_ns if st else None,
            local_size=st.st_size if st else None, local_md5=md5,
            remote_version=rf.version, remote_md5=md5,
            remote_size=st.st_size if st else None,
            sync_status="synced" if md5 else "pending_upload",
            error_msg=None if md5 else "기준선(해시) 미확정 — 다음 실행에서 재확인",
            last_synced_at=now_iso(),
        ))
        # 실제로 나간 바이트를 집계에 반영한다. 빠뜨리면 요약이 '올림 0B'라고 말해
        # 사용자가 데이터가 나가지 않은 것으로 읽는다.
        self.report.bytes_up += int(st.st_size if st else 0)

    def _free_conflict_rel(self, rel: str) -> str:
        """비어 있는 충돌 사본 경로를 고른다. 같은 분에 두 번 나면 뒤에 번호를 붙인다."""
        cand = conflict_copy_name(rel, self._now())
        if _stat_or_none(local_path(self.root, cand)) is None:
            return cand
        head, _, name = cand.rpartition("/")
        stem, dot, ext = name.rpartition(".")
        for i in range(2, 100):
            nm = (f"{stem} ({i}){dot}{ext}" if dot else f"{name} ({i})")
            alt = f"{head}/{nm}" if head else nm
            if _stat_or_none(local_path(self.root, alt)) is None:
                return alt
        raise RuntimeError(f"충돌 사본 이름을 만들지 못했습니다: {rel}")

    def local_move(self, action) -> None:
        """결정표 12: 원격 이동/개명을 로컬에 반영."""
        d = action.decision
        entry_local = d.local if d else None
        r = d.remote if d else None
        if entry_local is None or r is None:
            raise RuntimeError("이동에 필요한 정보가 없습니다")

        src = Path(entry_local.disk_path) if entry_local.disk_path else local_path(self.root, action.rel_path)
        dest = local_path(self.root, r.rel_path)
        if _stat_or_none(dest) is not None:
            raise LocalChangedDuringSync(
                f"이동 대상 경로에 이미 파일이 있습니다 — 옮기지 않았습니다: {r.rel_path}")

        jid = self.journal.begin(KIND_LOCAL_MOVE, action.rel_path, file_id=r.file_id,
                                 detail={"to": r.rel_path, "is_dir": r.is_dir})
        self.journal.phase(jid, "started")
        os.makedirs(ext_path(dest.parent), exist_ok=True)
        os.replace(ext_path(src), ext_path(dest))
        self.journal.phase(jid, "transferred", {"to": r.rel_path})

        old_key = action.key
        rec = self.base.get(old_key)
        fresh = _stat_or_none(dest)

        # 이동은 **파일을 옮길 뿐 내용을 받아오지 않는다.** 원격 내용까지 바뀐 경우
        # (결정표 13) 여기서 새 remote_version을 확정해 버리면, 뒤따르는 DOWNLOAD_UPDATE가
        # 실패했을 때 base가 '이미 받은 것'으로 굳어 그 수정이 영원히 도달하지 않는다.
        # --full 재조정으로도 못 잡는다(version이 같아 변경 없음으로 보인다).
        # 그래서 내용이 바뀐 경우에는 **옛 원격 메타를 그대로 유지**해 다음 패스가
        # 새 경로에서 결정표 5로 다시 잡게 한다.
        content_pending = bool(rec is not None and not r.is_dir and _remote_changed(rec, r))

        # base는 '마지막 동기화 시점의 로컬 내용'이어야 한다(I4). 편집된 파일을
        # 이동시킨 경우(원격이 자리만 옮긴 결정표 13) 이동 후 실측 stat과 옛 해시를
        # 섞어 기록하면 meta 게이트가 '변경 없음'으로 오판해 그 편집이 영원히
        # 올라가지 않는다(UT-12 실측, 2026-08-04). 옛 기준선이 온전하면 세 값을
        # 그대로 옮기고, 다음 스캔의 meta 불일치 → 해시 재계산 → 결정표 4에 맡긴다.
        keep_old_baseline = bool(
            rec is not None and not r.is_dir and rec.local_md5
            and rec.local_mtime_ns is not None and rec.local_size is not None)

        def _apply() -> None:
            if r.is_dir:
                # 폴더는 하위 레코드 경로까지 같은 트랜잭션에서 옮긴다 — 남겨 두면
                # 다음 스캔에서 하위 전체가 '삭제 + 신규'로 보인다.
                self.store.move_subtree(self.drive_id, action.rel_path, r.rel_path)
            else:
                try:
                    self.store.move_record(self.drive_id, old_key, r.rel_path)
                except KeyError:
                    pass        # base에 없던 항목 — 아래 upsert가 만든다
            self.store.upsert_file(FileRecord(
                drive_id=self.drive_id, rel_path=r.rel_path, file_id=r.file_id or None,
                parent_id=r.parent_id or None, server_name=r.server_name or None,
                is_dir=r.is_dir,
                local_mtime_ns=(rec.local_mtime_ns if keep_old_baseline
                                else (fresh.st_mtime_ns if fresh else None)),
                local_size=(rec.local_size if keep_old_baseline
                            else (fresh.st_size if fresh else None)),
                local_md5=(rec.local_md5 if rec else None) or entry_local.md5,
                # 순수 이동이면 전진, 내용까지 바뀌었으면 옛 값 유지(위 주석 참조)
                remote_revision=(rec.remote_revision if content_pending else r.revision),
                remote_version=(rec.remote_version if content_pending else r.version),
                remote_md5=((rec.remote_md5 if content_pending else r.md5)
                            or (rec.remote_md5 if rec else None)),
                remote_size=(rec.remote_size if content_pending else r.size),
                sync_status="pending_download" if content_pending else "synced",
                error_msg=("이동은 반영됨 — 원격 내용 수신 대기" if content_pending else None),
                last_synced_at=now_iso(),
            ))

        self.journal.commit(jid, _apply)
        # 메모리 base도 새 경로로 옮긴다 — 같은 실행의 뒤 항목이 옛 키를 보면 안 된다.
        self.base.pop(old_key, None)
        moved_rec = self.store.get_by_path(self.drive_id, r.rel_path)
        if moved_rec is not None:
            self.base[r.rel_path_key] = moved_rec
        self.report.tick(KIND_LOCAL_MOVE)

    def remote_move(self, action) -> None:
        """결정표 11: 로컬 이동/개명을 원격에 반영."""
        d = action.decision
        rec = (d.base if d else None) or self.base.get(action.key)
        entry_local = d.local if d else None
        if rec is None or not rec.file_id or entry_local is None:
            raise RuntimeError("원격 이동에 필요한 정보가 없습니다")

        new_rel = action.new_rel_path or entry_local.rel_path
        new_parent_rel, _, new_name = new_rel.rpartition("/")
        old_parent_rel = action.rel_path.rpartition("/")[0]

        jid = self.journal.begin(KIND_REMOTE_MOVE, action.rel_path, file_id=rec.file_id,
                                 detail={"to": new_rel})
        self.journal.phase(jid, "started")

        new_parent_id = (self.ensure_remote_folder(new_parent_rel)
                         if new_parent_rel else self.root_id)
        if path_key(new_parent_rel) != path_key(old_parent_rel):
            self.drive.move(self.drive_id, rec.file_id, new_parent_id)
        server_name = to_nfc(rec.server_name or "")
        if server_name != to_nfc(new_name):
            self.drive.rename(self.drive_id, rec.file_id, new_name)
            server_name = to_nfc(new_name)
            if server_name_will_differ(new_name):
                # R14: 서버가 앞뒤 공백을 절삭한다. 요청한 이름을 정본으로 기록하면
                # 다음 실행이 매번 '이름이 다르다'며 또 옮기려 한다 — 되물어 확정한다.
                try:
                    server_name = to_nfc(
                        self.drive.get_file_meta(self.drive_id, rec.file_id).name) or server_name
                except DoorayApiError as exc:
                    self.log.info("개명 후 저장명 확인 실패(요청명 사용) %s: %s", new_rel, exc)
        self.journal.phase(jid, "transferred", {"to": new_rel})

        old_key = action.key

        def _apply() -> None:
            try:
                self.store.move_record(self.drive_id, old_key, new_rel)
            except KeyError:
                pass
            self.store.upsert_file(FileRecord(
                drive_id=self.drive_id, rel_path=new_rel, file_id=rec.file_id,
                parent_id=new_parent_id, server_name=server_name or new_name, is_dir=False,
                local_mtime_ns=entry_local.mtime_ns, local_size=entry_local.size,
                local_md5=entry_local.md5 or rec.local_md5,
                remote_revision=rec.remote_revision, remote_version=rec.remote_version,
                remote_md5=rec.remote_md5, remote_size=rec.remote_size,
                sync_status="synced", last_synced_at=now_iso(),
            ))

        self.journal.commit(jid, _apply)
        self.base.pop(old_key, None)
        moved_rec = self.store.get_by_path(self.drive_id, new_rel)
        if moved_rec is not None:
            self.base[path_key(new_rel)] = moved_rec
        self.report.tick(KIND_REMOTE_MOVE)

    def _verify_unchanged_before_trash(self, action) -> None:
        """삭제 직전 재-stat (C2의 삭제판).

        삭제는 계획의 **맨 마지막**에 실행된다(planner._ORDER). 그 앞의 대용량 전송이
        수 분 걸리는 동안 사용자가 그 파일을 열어 저장했을 수 있다. 스캔 시점 상태와
        다르면 지우지 않고 보호로 강등한다 — 결정표 9의 '보존 승리'가 스캔 이후의
        편집에도 적용되어야 하기 때문이다.

        폴더는 하위에 재귀 적용되므로 **하위 전체**를 확인한다. 스캔에 없던 파일이
        새로 생겼거나(사용자가 방금 만든 것), 스캔 이후 수정된 파일이 하나라도 있으면
        그 폴더는 통째로 건드리지 않는다.
        """
        target = local_path(self.root, action.rel_path)

        # 스캔이 이 경로를 확인하지 못했다면 삭제 판단의 근거가 없다(I1). walk가 같은
        # 실패를 재현해 주기를 기대하지 않는 독립 방어선이다.
        if self.local_unobserved:
            k = action.key
            if any(k == u or k.startswith(u + "/") or u.startswith(k + "/")
                   for u in self.local_unobserved):
                raise LocalChangedDuringSync(
                    f"스캔이 확인하지 못한 경로가 포함돼 지우지 않았습니다: {action.rel_path}")

        if not action.is_dir:
            scanned = action.decision.local if action.decision else None
            try:
                st = os.stat(ext_path(target))
            except FileNotFoundError:
                return                      # 이미 없다 — 삭제 목적은 달성된 상태
            except OSError as exc:
                # 권한·잠금으로 상태를 못 읽는 것은 '없음'이 아니다. 확인 없이 지우지 않는다.
                raise LocalChangedDuringSync(
                    f"삭제 직전 상태를 읽지 못해 지우지 않았습니다: {action.rel_path} ({exc})"
                ) from exc
            if scanned is None or scanned.mtime_ns is None or scanned.size is None:
                raise LocalChangedDuringSync(
                    f"삭제 직전 상태를 확인할 수 없어 지우지 않았습니다: {action.rel_path}")
            if (st.st_mtime_ns, st.st_size) != (scanned.mtime_ns, scanned.size):
                raise LocalChangedDuringSync(
                    f"삭제 직전 로컬이 변경됨 — 지우지 않았습니다: {action.rel_path}")
            return

        snapshot = self.local_snapshot or {}

        def _walk_failed(exc: OSError) -> None:
            # os.walk는 기본적으로 오류를 **조용히 건너뛴다**. 하위를 못 읽은 채 통과하면
            # 확인되지 않은 파일이 그대로 휴지통으로 간다 — 이 가드의 존재 이유가 사라진다.
            raise LocalChangedDuringSync(
                f"하위 목록을 읽을 수 없어 폴더를 지우지 않았습니다: "
                f"{getattr(exc, 'filename', action.rel_path)} ({exc})")

        if not os.path.isdir(ext_path(target)):
            return                          # 폴더가 이미 없다

        for dirpath, _dirnames, filenames in os.walk(ext_path(target), onerror=_walk_failed):
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    rel = rel_posix(self.root, full)
                except ValueError:
                    continue
                entry = snapshot.get(path_key(rel))
                st = _stat_or_none(full)
                if entry is None:
                    raise LocalChangedDuringSync(
                        f"스캔 이후 생긴 파일이 있어 폴더를 지우지 않았습니다: {rel}")
                if st is not None and (st.st_mtime_ns, st.st_size) != (entry.mtime_ns, entry.size):
                    raise LocalChangedDuringSync(
                        f"스캔 이후 수정된 파일이 있어 폴더를 지우지 않았습니다: {rel}")

    def local_trash(self, action) -> None:
        """원격에서 지워진 것을 로컬 휴지통으로. 영구삭제는 하지 않는다(I5)."""
        target = local_path(self.root, action.rel_path)
        self._verify_unchanged_before_trash(action)
        jid = self.journal.begin(KIND_LOCAL_TRASH, action.rel_path,
                                 detail={"is_dir": action.is_dir})
        self.journal.phase(jid, "started")
        send_to_trash(target)
        self.journal.phase(jid, "transferred")
        key = action.key
        is_dir = action.is_dir

        def _apply() -> None:
            if is_dir:
                self.store.delete_subtree(self.drive_id, key)
            else:
                self.store.delete_by_key(self.drive_id, key)

        self.journal.commit(jid, _apply)
        self.base.pop(key, None)
        self.report.tick(KIND_LOCAL_TRASH)

    def remote_trash(self, action) -> None:
        """로컬에서 지워진 것을 원격 휴지통으로. 영구삭제 API는 쓰지 않는다(I5)."""
        rec = (action.decision.base if action.decision else None) or self.base.get(action.key)
        if rec is None or not rec.file_id:
            # 원격 id를 모르면 지울 수 없다 — 기록만 정리한다.
            self.store.delete_by_key(self.drive_id, action.key)
            self.base.pop(action.key, None)
            return
        jid = self.journal.begin(KIND_REMOTE_TRASH, action.rel_path, file_id=rec.file_id,
                                 detail={"is_dir": action.is_dir})
        self.journal.phase(jid, "started")
        try:
            self.drive.move_to_trash(self.drive_id, rec.file_id)
        except DoorayApiError as exc:
            # 실측: 이미 휴지통에 있으면 HTTP 200 + resultCode=-15700100으로 실패한다.
            # 상위 폴더가 먼저 휴지통에 들어가면 자식이 이 상태가 된다 — 정상 처리한다.
            if exc.result_code != NO_ACCESS_AUTHORITY:
                raise
            self.log.info("이미 휴지통에 있는 항목입니다: %s", action.rel_path)
        self.journal.phase(jid, "transferred")
        key = action.key
        is_dir = action.is_dir

        def _apply() -> None:
            if is_dir:
                self.store.delete_subtree(self.drive_id, key)
            else:
                self.store.delete_by_key(self.drive_id, key)

        self.journal.commit(jid, _apply)
        self.base.pop(key, None)
        self.report.tick(KIND_REMOTE_TRASH)

    def touch_base(self, action) -> None:
        """전송 없이 기준선만 맞춘다(내용이 같음이 확인된 경우)."""
        d = action.decision
        entry_local = d.local if d else None
        r = d.remote if d else None
        rec = self.base.get(action.key)
        fresh = _stat_or_none(local_path(self.root, action.rel_path))
        new = FileRecord(
            drive_id=self.drive_id, rel_path=action.rel_path,
            file_id=(r.file_id if r else (rec.file_id if rec else None)) or None,
            parent_id=(r.parent_id if r else (rec.parent_id if rec else None)) or None,
            server_name=(r.server_name if r else (rec.server_name if rec else None)) or None,
            is_dir=action.is_dir,
            local_mtime_ns=(entry_local.mtime_ns if entry_local
                            else (fresh.st_mtime_ns if fresh else None)),
            local_size=(entry_local.size if entry_local else (fresh.st_size if fresh else None)),
            local_md5=(entry_local.md5 if entry_local else (rec.local_md5 if rec else None)),
            remote_revision=(r.revision if r else (rec.remote_revision if rec else None)),
            remote_version=(r.version if r else (rec.remote_version if rec else None)),
            remote_md5=((r.md5 if r else None)
                        or (entry_local.md5 if entry_local else None)
                        or (rec.remote_md5 if rec else None)),
            remote_size=(r.size if r else (rec.remote_size if rec else None)),
            sync_status="synced", last_synced_at=now_iso(),
        )
        self._save(new)
        self.report.tick(KIND_TOUCH_BASE)

    def forget(self, action) -> None:
        """양쪽에서 사라진 항목의 기록만 정리한다. 파일에는 손대지 않는다."""
        if action.is_dir:
            self.store.delete_subtree(self.drive_id, action.key)
        else:
            self.store.delete_by_key(self.drive_id, action.key)
        self.base.pop(action.key, None)
        self.report.tick(KIND_FORGET)

    # ------------------------------------------------------------------ 보조
    def _save(self, rec: FileRecord) -> None:
        self.store.upsert_file(rec)
        self.base[rec.rel_path_key or path_key(rec.rel_path)] = rec

    def _mark(self, rel: str, is_dir: bool, status: str, reason: str,
              action=None) -> None:
        """실패·보호를 DB에 남긴다. base 값(local_*/remote_*)은 손대지 않는다."""
        rec = self.store.get_by_path(self.drive_id, rel)
        created = rec is None
        if rec is None:
            rec = FileRecord(drive_id=self.drive_id, rel_path=rel, is_dir=is_dir)

        # 새로 만든 레코드에는 원격 id를 심어 둔다. 없으면 dirty 조회(file_id NOT NULL)에서
        # 빠져 델타 모드가 이 항목을 **영원히 재시도하지 않는다**(I6가 막으려던 구멍).
        # 단, 같은 file_id가 다른 경로에 이미 잡혀 있으면 심지 않는다 — 결정표 13처럼
        # 옛 경로 레코드가 같은 id를 들고 있는 경우 중복 행을 만들기 때문이다.
        if created and not rec.file_id and action is not None:
            r = getattr(action.decision, "remote", None) if action.decision else None
            fid = getattr(r, "file_id", "") if r is not None else ""
            if fid:
                twin = self.store.get_by_file_id(fid)
                if twin is None or path_key(twin.rel_path) == path_key(rel):
                    rec.file_id = fid
                    rec.parent_id = rec.parent_id or (r.parent_id or None)
                    rec.server_name = rec.server_name or (r.server_name or None)

        rec.sync_status = status
        rec.error_msg = str(reason)[:500]
        self.store.upsert_file(rec)
        self.base[path_key(rel)] = rec
