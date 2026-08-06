"""로컬 파일시스템 스냅샷 (모듈규약 §10).

3-way diff의 L(로컬) 축을 만든다. 성능의 핵심은 `needs_hash` — base 레코드와
(local_mtime_ns, local_size)가 같으면 MD5를 계산하지 않는다. 대용량 폴더에서
스캔 비용은 사실상 전부 해시 IO이므로 이 게이트가 실질적인 성능 결정 요인이다.

안전 규칙:
- 모든 FS 접근은 `ext_path` 경유(규약 §12-4). 260자를 넘는 경로가 실제로 존재한다.
- 심볼릭 링크/junction 디렉터리는 순회하지 않는다 — 무한 루프와 동기화 루트 밖
  데이터 유입을 동시에 막는다.
- 접근 거부·사용 중 파일은 **해당 항목만** 건너뛰고 경고를 남긴다. 스캔 전체를
  실패시키면 폴더 하나 때문에 동기화가 멈춘다.
"""
from __future__ import annotations

import logging
import os
import stat as _stat
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..util.hashing import md5_file
from ..util.paths import ext_path, local_path, matches_any, path_key, rel_posix

if TYPE_CHECKING:  # 타입 전용 — 순수 FS 모듈에 DB 계층 런타임 의존을 만들지 않는다
    from ..store.db import FileRecord

log = logging.getLogger(__name__)

# Windows 재분석 지점 태그. os.path.isjunction은 3.12+라 직접 판별한다.
# DirEntry.is_symlink()는 IO_REPARSE_TAG_SYMLINK만 True이므로 junction을 놓친다.
_TAG_MOUNT_POINT = 0xA0000003
_TAG_SYMLINK = 0xA000000C

# 사용자 exclude와 무관하게 항상 제외.
# 'synchere.bat'은 각 동기화 폴더에 놓아 쓰는 실행 도구다(더블클릭 = 그 폴더 동기화).
# 도구 자신이 전송 대상이 되면 안 되므로 양축(스캐너·RemoteCollector) 모두에서 제외한다
# — 2026-08-07 사용자 요구. 원격 축은 RemoteCollector가 이 튜플을 가져다 쓴다.
# '.dooraysync_tmp'는 원자적 다운로드(C1)가 대상 폴더마다 만드는 임시 디렉터리다 —
# 스캔에 잡히면 다음 push에서 원격으로 역류한다.
ALWAYS_EXCLUDE: tuple[str, ...] = (".dooraysync/", ".dooraysync_tmp/", "synchere.bat")


@dataclass
class LocalEntry:
    rel_path: str            # '/' 구분자, NFC — 비교·DB 키·원격 이름 전용
    rel_path_key: str
    is_dir: bool
    # 디스크상의 실제 절대경로. FS 접근은 **반드시** 이 값만 쓴다.
    # NTFS는 파일명을 정규화하지 않고 기록된 코드포인트 그대로 보관하는데,
    # `\\?\` 접두는 Win32의 경로 정규화를 끄기 때문에 NFC로 바꾼 rel_path로는
    # NFD로 저장된 이름의 파일을 열 수 없다(맥에서 만든 한글 파일명이 대표적).
    disk_path: str = ""
    mtime_ns: int | None = None
    size: int | None = None
    md5: str | None = None   # 필요할 때만 계산 (needs_hash → fill_md5)

    def fs_path(self, root: Path) -> str:
        """FS 접근용 경로. disk_path가 있으면 그것을, 없으면 rel_path에서 복원."""
        if self.disk_path:
            return ext_path(self.disk_path)
        return ext_path(local_path(root, self.rel_path))


@dataclass(frozen=True)
class SkippedItem:
    """스캔에서 제외된 항목. 건너뛴 사실을 호출측이 리포트할 수 있게 남긴다."""

    rel_path: str
    reason: str


def _is_reparse(st: os.stat_result) -> bool:
    """심볼릭 링크 또는 junction인지.

    OneDrive 같은 클라우드 자리표시자도 재분석 지점이지만 태그가 달라 여기서
    걸리지 않는다 — 그것들은 평범한 파일로 다뤄야 한다.
    """
    tag = getattr(st, "st_reparse_tag", 0)  # 비Windows에는 없는 필드
    if tag:
        return tag in (_TAG_SYMLINK, _TAG_MOUNT_POINT)
    return _stat.S_ISLNK(st.st_mode)


class LocalScanner:
    """동기화 루트 아래 로컬 스냅샷 생성기.

    logger는 규약 외 선택 인자다(생략 시 모듈 로거). 위치 인자 시그니처는 그대로다.
    """

    def __init__(self, root: Path, exclude: Sequence[str],
                 *, logger: logging.Logger | None = None) -> None:
        self.root = Path(root)
        self.exclude: tuple[str, ...] = ALWAYS_EXCLUDE + tuple(p for p in (exclude or ()) if p)
        self.log = logger or log
        self.skipped: list[SkippedItem] = []

    # ---------- 스캔 ----------
    def scan(self) -> dict[str, LocalEntry]:
        """rel_path_key -> LocalEntry. exclude 적용, 링크 미추적, 접근 불가 항목은 건너뜀.

        반환 dict의 삽입 순서는 부모 디렉터리가 항상 자식보다 앞선다
        (planner의 '폴더 먼저' 정렬 전제).
        """
        root_ext = ext_path(self.root)
        if not os.path.isdir(root_ext):
            raise FileNotFoundError(f"동기화 루트가 없거나 디렉터리가 아닙니다: {self.root}")

        self.skipped = []
        out: dict[str, LocalEntry] = {}
        queue: deque[tuple[str, str]] = deque([("", root_ext)])
        while queue:
            rel_dir, dir_ext = queue.popleft()
            for entry, st in self._children(rel_dir, dir_ext):
                try:
                    rel = rel_posix(self.root, Path(entry.path))
                except ValueError as exc:  # 루트 밖 — 링크를 안 따라가므로 정상적으로는 불가
                    self._skip(entry.name, f"루트 밖 경로: {exc}")
                    continue

                if self.is_excluded(rel):
                    # 디렉터리면 큐에 넣지 않으므로 하위 트리 전체가 함께 제외된다
                    continue
                if _is_reparse(st):
                    self._skip(rel, "심볼릭 링크/junction — 따라가지 않음")
                    continue

                key = path_key(rel)
                prev = out.get(key)
                if prev is not None:
                    # NFC/NFD 또는 대소문자만 다른 이름이 공존하면 키가 겹친다.
                    # 조용히 덮어쓰면 한쪽이 사라지므로 먼저 본 쪽을 유지하고 보고한다.
                    self._skip(rel, f"경로 키 충돌 — 먼저 스캔된 {prev.rel_path!r} 유지")
                    continue

                if _stat.S_ISDIR(st.st_mode):
                    out[key] = LocalEntry(rel_path=rel, rel_path_key=key, is_dir=True,
                                          disk_path=entry.path)
                    queue.append((rel, entry.path))
                    continue

                fresh = self._stat_file(rel, entry.path)
                if fresh is None:
                    continue
                out[key] = LocalEntry(
                    rel_path=rel, rel_path_key=key, is_dir=False,
                    disk_path=entry.path,
                    mtime_ns=fresh.st_mtime_ns, size=fresh.st_size,
                )
        return out

    def is_excluded(self, rel_path: str) -> bool:
        """exclude 패턴 매칭(util.paths.matches_any).

        디렉터리가 매치되면 scan이 그 하위를 순회하지 않으므로 하위 전체가 제외된다.
        """
        return matches_any(rel_path, self.exclude)

    # ---------- 해시 ----------
    def needs_hash(self, entry: LocalEntry, rec: FileRecord | None) -> bool:
        """base 레코드와 (mtime_ns, size)가 같으면 False — 해시 계산 생략(성능)."""
        if entry.is_dir or entry.md5:
            return False
        if entry.mtime_ns is None or entry.size is None:
            # 메타를 못 읽은 항목은 내용으로만 판정할 수 있다
            return True
        if rec is None or rec.is_dir or not rec.local_md5:
            return True
        if rec.local_mtime_ns is None or rec.local_size is None:
            return True
        return not (int(rec.local_mtime_ns) == entry.mtime_ns and int(rec.local_size) == entry.size)

    def fill_md5(self, entry: LocalEntry) -> LocalEntry:
        """md5를 계산한 새 LocalEntry 반환. 디렉터리·이미 계산된 항목은 그대로 반환.

        읽기 실패는 삼키지 않고 OSError로 올린다 — scan과 달리 이 시점의 실패는
        '해당 파일을 전송할 수 없다'는 뜻이라 호출측이 error/skip을 판단해야 한다.
        """
        if entry.is_dir or entry.md5:
            return entry
        # disk_path(디스크 원본 표기)로 연다 — rel_path는 NFC로 정규화돼 있어
        # NFD로 저장된 이름에는 접근할 수 없다.
        return replace(entry, md5=md5_file(entry.fs_path(self.root)))

    # ---------- 내부 ----------
    def _children(self, rel_dir: str, dir_ext: str) -> Iterator[tuple[os.DirEntry[str], os.stat_result]]:
        """디렉터리 한 겹을 (DirEntry, lstat)로 나열. 실패는 경고만 남기고 계속 진행."""
        label = rel_dir or "(루트)"
        try:
            it = os.scandir(dir_ext)
        except OSError as exc:
            self._skip(label, f"디렉터리 열기 실패: {_desc(exc)}", exc=exc)
            return
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    return
                except OSError as exc:
                    # 나열 도중 실패하면 남은 항목을 보장할 수 없다 — 이 디렉터리만 중단.
                    # (하위를 '삭제됨'으로 오판하지 않도록 호출측은 skipped를 확인해야 한다)
                    self._skip(label, f"디렉터리 나열 중단: {_desc(exc)}", exc=exc)
                    return
                try:
                    # follow_symlinks=False: 링크 자체의 속성이 필요하다(재분석 지점 판별)
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    self._skip(_join(rel_dir, entry.name), f"stat 실패: {_desc(exc)}", exc=exc)
                    continue
                yield entry, st

    def _stat_file(self, rel: str, full_ext: str) -> os.stat_result | None:
        """파일 메타는 scandir 캐시가 아니라 다시 stat한다.

        NTFS는 열려 있는 파일의 크기·시각을 디렉터리 항목에 즉시 반영하지 않는다.
        낡은 값으로 base를 기록하면 다음 스캔에서 (mtime,size)가 달라져 불필요한
        재해시·재전송이 발생한다. 파일당 stat 1회는 해시 IO에 비하면 무시할 만하다.
        """
        try:
            return os.stat(full_ext)
        except OSError as exc:
            self._skip(rel, f"파일 stat 실패: {_desc(exc)}", exc=exc)
            return None

    def _skip(self, rel_path: str, reason: str, *, exc: BaseException | None = None) -> None:
        self.skipped.append(SkippedItem(rel_path=rel_path, reason=reason))
        # 임시파일이 스캔 도중 사라지는 것은 흔한 일이라 경고로 올리지 않는다
        level = logging.DEBUG if isinstance(exc, FileNotFoundError) else logging.WARNING
        self.log.log(level, "스캔 건너뜀: %s — %s", rel_path or "(루트)", reason)


def _join(rel_dir: str, name: str) -> str:
    """진단 메시지용 경로 조합(정본 rel_path는 rel_posix가 만든다)."""
    return f"{rel_dir}/{name}" if rel_dir else name


def _desc(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"
