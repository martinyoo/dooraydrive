"""저널과 크래시 복구 — 규약_M2 §5.

M2의 완료 기준은 "전송 중 강제 종료 후 재실행 시 무손실 수렴"이다. 그 성질은 저널이
많은 것을 아는 데서 오지 않는다. **base를 추정으로 채우지 않는 데서** 온다.

복구가 하는 일은 세 가지뿐이다.

1. 남은 임시파일(`.dooraysync_tmp/*.part`)을 지운다 — 사용자 데이터가 아니다.
2. 중단된 항목의 **sync_status만** 재검사 대상으로 바꾼다. local_*/remote_* 값은 손대지 않는다.
3. 저널 항목을 'failed'로 종결해 다음 실행이 같은 항목을 또 보지 않게 한다.

그러면 다음 diff가 '실제 로컬 + 실제 원격'을 다시 비교해 알아서 수렴한다. 복구가 상태를
복원하려 들면 오히려 반쪽짜리 base를 만들어 조용한 손실을 낳는다(규약_M2 I4).
"""
from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..store.db import FileRecord, Store, now_iso
from ..util.paths import ext_path

__all__ = ["SyncJournal", "RecoveryReport", "recover", "sweep_tmp"]

log = logging.getLogger(__name__)

# 어느 방향의 작업이었는지 → 재검사 시 어느 쪽을 의심할지
_UPLOAD_OPS = ("UPLOAD_NEW", "UPLOAD_VERSION", "REMOTE_MOVE", "MKDIR_REMOTE", "REMOTE_TRASH")
_DOWNLOAD_OPS = ("DOWNLOAD_NEW", "DOWNLOAD_UPDATE", "CONFLICT", "LOCAL_MOVE",
                 "MKDIR_LOCAL", "LOCAL_TRASH")


@dataclass
class RecoveryReport:
    scanned: int = 0
    tmp_removed: int = 0
    marked: list[tuple[str, str]] = field(default_factory=list)
    conflicts_kept: list[str] = field(default_factory=list)
    closed: int = 0

    @property
    def had_work(self) -> bool:
        return bool(self.scanned)


class SyncJournal:
    """저널 기록기. Store.journal_* 위에 얇게 올린다.

    executor는 **반드시** `commit()`을 통해서만 base를 갱신한다 — 저널 'committed'와 files
    갱신이 한 트랜잭션이어야 크래시가 반쪽 base를 남기지 않는다.
    """

    def __init__(self, store: Store, session: str | None = None) -> None:
        self.store = store
        self.session = session or uuid.uuid4().hex

    def begin(self, op: str, rel_path: str, *, file_id: str | None = None,
              detail: dict | None = None) -> int:
        return self.store.journal_begin(self.session, op, file_id=file_id,
                                        rel_path=rel_path, detail=detail)

    def phase(self, entry_id: int, phase: str, detail: dict | None = None) -> None:
        self.store.journal_phase(entry_id, phase, detail)

    def commit(self, entry_id: int, apply: Callable[[], None],
               detail: dict | None = None) -> None:
        """base 갱신과 'committed' 기록을 한 트랜잭션으로 묶는다(규약_M2 I4)."""
        with self.store.transaction():
            apply()
            self.store.journal_phase(entry_id, "committed", detail)

    def fail(self, entry_id: int, reason: str) -> None:
        self.store.journal_phase(entry_id, "failed", {"reason": str(reason)[:500]})


def _remove_tmp(path: str | None) -> bool:
    """임시파일 제거. **사용자 파일에는 절대 쓰지 않는다** — 호출측이 경로를 검증한다."""
    if not path:
        return False
    try:
        os.remove(ext_path(path))
        return True
    except OSError:
        return False


def _is_tmp_path(path: str, root: Path) -> bool:
    r"""정말 우리가 만든 임시파일인지. `.dooraysync_tmp` 아래의 `.part`만 허용한다.

    저널 detail은 우리가 쓴 값이지만, 여기서 한 번 더 검사하는 이유는 이 함수가
    **삭제**를 하기 때문이다. 조건이 틀리면 사용자 파일을 지운다.
    """
    p = str(path or "").replace("/", "\\")
    if not p.lower().endswith(".part"):
        return False
    if ".dooraysync_tmp" not in p.lower():
        return False
    try:
        root_s = str(Path(root).resolve()).lower()
    except OSError:
        root_s = str(root).lower()
    return p.lower().startswith(root_s)


def sweep_tmp(root: Path, *, logger: logging.Logger | None = None) -> int:
    """동기화 루트 아래 남은 `.dooraysync_tmp/*.part` 를 지운다. 반환: 지운 개수.

    저널에 기록된 것만 지우면 **기록 직전에 죽은 전송의 찌꺼기가 영구히 남는다**
    (실계정에서 확인). `.part`는 우리가 만든 임시파일이고 이 함수는 단일 인스턴스
    잠금 안에서만 불리므로, 다른 전송이 쓰는 중일 수 없다.
    """
    lg = logger or log
    n = 0
    try:
        candidates = list(Path(root).rglob(".dooraysync_tmp/*.part"))
    except OSError as exc:
        lg.debug("임시파일 청소 중 순회 실패: %s", exc)
        return 0
    for p in candidates:
        if _remove_tmp(str(p)):
            n += 1
    if n:
        lg.info("남은 임시파일 %d건을 정리했습니다", n)
    return n


def recover(store: Store, drive_id: str, root: Path, *,
            logger: logging.Logger | None = None) -> RecoveryReport:
    """미완료 저널 항목을 정리한다. **단일 인스턴스 잠금 안에서만 호출한다.**"""
    lg = logger or log
    rep = RecoveryReport()
    # 저널 항목이 없어도 찌꺼기는 남을 수 있다(기록 전에 죽은 경우) — 먼저 쓸어 낸다.
    rep.tmp_removed += sweep_tmp(root, logger=lg)
    pending = list(store.iter_incomplete())
    if not pending:
        return rep

    known_copies = {c.get("local_copy_path") for c in store.iter_unresolved()}

    for item in pending:
        rep.scanned += 1
        op = str(item.get("op") or "")
        rel = str(item.get("rel_path") or "")
        detail = item.get("detail") or {}

        tmp = detail.get("tmp")
        if tmp and _is_tmp_path(str(tmp), root) and _remove_tmp(str(tmp)):
            rep.tmp_removed += 1

        # 충돌 사본은 사용자 데이터다 — 지우지 않는다. 기록이 없으면 남긴다.
        copy_path = detail.get("conflict_copy")
        if copy_path:
            rep.conflicts_kept.append(str(copy_path))
            if copy_path not in known_copies and os.path.exists(ext_path(str(copy_path))):
                store.add_conflict(rel or str(copy_path), "중단된 충돌 처리", str(copy_path))
                known_copies.add(copy_path)

        if rel:
            status = ("pending_upload" if op in _UPLOAD_OPS
                      else "pending_download" if op in _DOWNLOAD_OPS else "error")
            rec = store.get_by_path(drive_id, rel)
            if rec is None:
                rec = FileRecord(drive_id=drive_id, rel_path=rel,
                                 is_dir=bool(detail.get("is_dir")))
            # 값은 그대로 두고 상태만 바꾼다 — 추정으로 base를 채우지 않는다(I4).
            rec.sync_status = status
            rec.error_msg = (f"이전 실행이 '{item.get('phase')}' 단계에서 중단됨"
                             f"({op}) — 다음 동기화에서 재검사")
            rec.last_synced_at = rec.last_synced_at or now_iso()
            store.upsert_file(rec)
            rep.marked.append((rel, status))

        store.journal_phase(int(item["entry_id"]), "failed", {"recovered": True, "op": op})
        rep.closed += 1

    lg.warning("중단된 작업 %d건을 복구 표시했습니다 (임시파일 %d건 정리)",
               rep.scanned, rep.tmp_removed)
    return rep
