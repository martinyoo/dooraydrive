"""원격 상태 축(R) 수집 — 규약_M2 §2 (강제 규칙 B1~B5).

3-way diff의 R을 만든다. 경로는 두 가지다.

- `full()`  : 목록 API 전체 순회. **완전한** 스냅샷(is_complete=True).
- `delta()` : changes 델타. **불완전한** 뷰다 — 이번에 관측한 항목만 안다.

이 구분이 M2 안전성의 핵심이다(규약_M2 I1). 델타 뷰에서 "entries에 없다"는
**삭제가 아니라 '이번에 확인하지 않았다'**는 뜻이며, 이것을 삭제로 읽으면 폴더 개명 한 번에
하위 전체가 사라진다(R8). 그래서 뷰가 자기 완전성을 `observed()`로 직접 답한다.

실측 근거: 검토보고서 §3.1(부분 페이지), §3.2(net-뷰), §3.3(시나리오별 changes 표현)
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from ..api.client import DoorayApiError
from ..api.drive import DriveAPI
from ..api.models import ChangeItem, Cursor, RemoteFile
from ..util.paths import join_remote, matches_any, name_issue, path_key, to_nfc

__all__ = [
    "RemoteEntry", "RemoteView", "RemoteCollector", "RemoteRootError",
    "rel_from_remote", "resolve_remote_root", "DEFAULT_PROBE_BUDGET",
]

log = logging.getLogger(__name__)

# 폴더 하나의 하위 재열람(B4) 상한. 넘으면 잘렸다고 표시하고 전체 재조정을 권한다.
_MAX_SUBTREE_ITEMS = 50_000

# 델타 패스가 미완료 레코드를 개별 조회로 확인할 수 있는 한 번의 상한(_probe_dirty).
# 이름을 붙여 밖으로 내보낸다 — 백로그가 이 수를 넘으면 델타로는 따라잡을 수 없고,
# 그 판단(전체 순회로 전환)은 CLI가 한다. 두 곳이 서로 다른 수를 쓰면 '전환했는데도
# 여전히 부분 처리' 같은 조용한 미수렴이 된다.
DEFAULT_PROBE_BUDGET = 500


class RemoteRootError(ValueError):
    """동기화 시작 폴더를 확정할 수 없음. CLI가 설정 오류(종료코드 2)로 옮긴다."""


@dataclass(frozen=True)
class RemoteEntry:
    """원격 항목 하나의 관측값. rel_path는 **동기화 루트 기준** 상대경로다."""

    rel_path: str
    rel_path_key: str
    is_dir: bool
    file_id: str
    parent_id: str = ""
    server_name: str = ""          # 서버 저장명 정본(R14)
    version: int | None = None
    revision: int | None = None
    size: int | None = None
    md5: str | None = None         # changes에만 존재. 목록 API 응답에는 없다(실측 PoC-02)

    @classmethod
    def from_remote_file(cls, rf: RemoteFile, rel_path: str) -> RemoteEntry:
        return cls(
            rel_path=rel_path,
            rel_path_key=path_key(rel_path),
            is_dir=rf.is_dir,
            file_id=rf.id or "",
            parent_id=rf.parent_id or "",
            server_name=to_nfc(rf.name),
            version=rf.version,
            revision=rf.revision or None,
            size=rf.size,
            md5=(rf.md5 or None),
        )

    @classmethod
    def from_change(cls, ci: ChangeItem, rel_path: str) -> RemoteEntry:
        return cls(
            rel_path=rel_path,
            rel_path_key=path_key(rel_path),
            is_dir=(ci.file_type == "folder"),
            file_id=ci.file_id or "",
            server_name=to_nfc(ci.name or ""),
            version=ci.version,
            revision=ci.revision or None,
            size=ci.size,
            md5=(ci.md5 or None),
        )


@dataclass
class RemoteView:
    """이번 패스에서 확인한 원격 상태.

    `is_complete=False`(델타)일 때 관측하지 않은 키에 대해서는 **아무 판단도 하지 않는다**.
    """

    entries: dict[str, RemoteEntry] = field(default_factory=dict)
    deleted_keys: set[str] = field(default_factory=set)
    moved_out_keys: set[str] = field(default_factory=set)   # 동기화 범위 밖으로 이동됨
    is_complete: bool = False
    cursor: Cursor = field(default_factory=Cursor)
    unsyncable: list[tuple[str, str]] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    truncated: bool = False
    probed_ids: set[str] = field(default_factory=set)
    probe_skipped: int = 0          # 예산을 넘겨 확인하지 못한 건수 — 반드시 보고할 것
    changes_seen: int = 0
    subtrees_relisted: int = 0

    def observed(self, key: str) -> bool:
        """이번 뷰가 그 키의 원격 상태를 실제로 확인했는가(규약_M2 I1)."""
        if self.is_complete:
            return True
        return (key in self.entries or key in self.deleted_keys
                or key in self.moved_out_keys)

    def by_file_id(self) -> dict[str, RemoteEntry]:
        """file_id → 항목. 원격 이동/개명 판정(결정표 12)의 근거."""
        return {e.file_id: e for e in self.entries.values() if e.file_id}


def rel_from_remote(full_path: str, remote_prefix: str = "") -> str:
    """원격 전체 경로('/a/b.txt') → 동기화 루트 기준 상대경로('b.txt').

    remote_prefix 하위만 동기화 대상이므로 접두를 떼어낸다. 접두 자신이나 접두 밖의
    경로는 ''를 돌려줘 호출측이 건너뛰게 한다.
    비교는 path_key(대소문자·정규화 무시) 기준이다 — 서버 표기가 'WORK'이고 설정이
    'Work'여도 같은 폴더로 봐야 한다(2026-08-02 실측 결함).
    """
    rel = to_nfc(str(full_path or "").replace("\\", "/").lstrip("/"))
    if not remote_prefix:
        return rel
    pref = to_nfc(str(remote_prefix).replace("\\", "/").strip("/"))
    if not pref:
        return rel
    key, pkey = path_key(rel), path_key(pref)
    if key == pkey:
        return ""                       # 접두 폴더 자신
    if key.startswith(pkey + "/"):
        # 실제 잘라내는 길이는 원본 문자열 기준이어야 한다(NFC 길이 = key 길이가 아닐 수 있다)
        return rel.split("/", len([c for c in pref.split("/") if c]))[-1]
    return ""


def resolve_remote_root(
    drive: DriveAPI, drive_id: str, remote_path: str, *, create: bool = False,
    on_create: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """동기화 시작 폴더를 정한다. (folder_id, 정규화된 원격 접두) 반환.

    폴더 탐색은 **대소문자를 무시**한다(DriveAPI.find_child_by_name의 폴백).
    실측 결함(2026-08-02): 로컬 표기 'Writing'으로 정확 일치만 찾으면 원격 'WRITING'을
    놓치고 새로 만들려 하는데, 서버의 중복 검사는 대소문자를 무시하므로 409로 거부된다.

    create=True는 init 계열에서만 쓴다 — 경로 오타로 엉뚱한 폴더를 만드는 사고를 막기 위해
    기본값은 False다.
    """
    root_id = drive.find_root_folder(drive_id)
    pref = to_nfc(str(remote_path or "").replace("\\", "/").strip("/"))
    if not pref:
        return root_id, ""

    cur = root_id
    resolved: list[str] = []
    for part in pref.split("/"):
        if not part:
            continue
        child = drive.find_child_by_name(drive_id, cur, part)
        if child is not None and not child.is_dir:
            raise RemoteRootError(
                f"원격에 같은 이름의 파일이 있어 폴더로 쓸 수 없습니다: {pref} ({part})")
        if child is None:
            if not create:
                raise RemoteRootError(
                    f"원격 폴더를 찾을 수 없습니다: {pref}  (막힌 지점: {part})\n"
                    f"  새로 만들려면 init에 --create-remote 를 붙이세요.")
            created, is_new = drive.create_folder_ex(drive_id, cur, part)
            if on_create is not None:
                on_create(created.name or part)
            cur = created.id
            resolved.append(to_nfc(created.name) or part)
            continue
        cur = child.id
        # 서버 표기를 정본으로 접두를 재조립한다 — 이후 모든 경로 계산이 이 접두를 뗀다.
        resolved.append(to_nfc(child.name) or part)
    return cur, "/".join(resolved)


@dataclass(frozen=True)
class RootResolution:
    root_id: str
    prefix: str
    followed: bool = False          # 앵커로 개명/이동을 추종했는가
    old_remote_path: str = ""       # 실패했던 설정값(알림용)


def resolve_remote_root_anchored(
    drive: DriveAPI, drive_id: str, remote_path: str, *,
    anchor_id: str | None = None, create: bool = False,
    on_create: Callable[[str], None] | None = None,
) -> RootResolution:
    """경로 해석이 실패하면 앵커(folder_id)로 폴더를 되찾는다 — 원격 개명·이동 추종.

    안전 규칙(전부 fail-closed — 하나라도 어긋나면 **원래 오류**를 그대로 올린다):
    - 앵커 meta 응답에 parent_path가 없으면 위치를 추정하지 않는다
      (_probe_dirty와 같은 원칙 — 실측상 부모 경로가 빈 응답이 존재한다).
    - 재구성한 경로를 resolve_remote_root로 **재해석해 검증**한다. 루트에서의 정상
      하강 탐색은 휴지통을 지나지 않으므로, 휴지통에 들어간 폴더는 여기서 걸러진다.
    - 재해석 결과 id가 앵커와 다르면 추종하지 않는다(대소문자 무시 서버에서 동명
      재생성 시나리오 — 이 한 줄이 빠지면 엉뚱한 폴더에 push하는 사고가 된다).
    """
    try:
        rid, pref = resolve_remote_root(drive, drive_id, remote_path,
                                        create=create, on_create=on_create)
        return RootResolution(rid, pref)
    except RemoteRootError as orig:
        if not anchor_id:
            raise
        try:
            rf = drive.get_file_meta(drive_id, anchor_id)
        except DoorayApiError as exc:
            log.info("앵커 조회 실패(원래 오류로 진행) anchor=%s: %s", anchor_id, exc)
            raise orig from None
        if not rf.id or not rf.is_dir or not rf.parent_path:
            raise orig from None
        candidate = rf.full_path.strip("/")
        if not candidate:
            raise orig from None
        try:
            rid, pref = resolve_remote_root(drive, drive_id, candidate)
        except RemoteRootError:
            raise orig from None        # 휴지통·도달 불가 — 추종 금지
        if rid != anchor_id:
            raise orig from None        # 재구성 경로가 다른 폴더 — fail-closed
        return RootResolution(rid, pref, followed=True,
                              old_remote_path=str(remote_path or ""))


class RemoteCollector:
    """원격 뷰 생성기. DriveAPI 호출은 전부 여기에 모은다."""

    def __init__(self, drive: DriveAPI, drive_id: str, remote_prefix: str, root_id: str,
                 *, exclude: Sequence[str] = (), logger: logging.Logger | None = None) -> None:
        self.drive = drive
        self.drive_id = drive_id
        self.prefix = to_nfc(str(remote_prefix or "").replace("\\", "/").strip("/"))
        self.root_id = root_id
        # 스캐너의 상시 제외(도구 파일 포함)를 원격 축에도 동일하게 적용한다 —
        # 로컬만 제외하면 원격 사본이 '원격 신규'로 보여 매번 되받으려 든다.
        from .scanner import ALWAYS_EXCLUDE
        self.exclude = ALWAYS_EXCLUDE + tuple(p for p in (exclude or ()) if p)
        self.log = logger or log

    # ------------------------------------------------------------------ 전체
    def full(self, *, on_item: Callable[[str], None] | None = None) -> RemoteView:
        """목록 API 전체 순회(B5). is_complete=True."""
        view = RemoteView(is_complete=True)
        base_path = ("/" + self.prefix) if self.prefix else ""
        bad_dirs: set[str] = set()
        seen: dict[str, str] = {}

        for rf, full in self.drive.walk(self.drive_id, self.root_id, base_path=base_path):
            rel = rel_from_remote(full, self.prefix)
            if not rel:
                continue
            if on_item is not None:
                on_item(rel)
            self._absorb(view, rf, rel, seen, bad_dirs)
        return view

    # ------------------------------------------------------------------ 델타
    def delta(self, cursor: Cursor, *, known_by_file_id: dict[str, str] | None = None,
              probe_file_ids: Iterable[str] = (), max_items: int = 20_000,
              max_probes: int = DEFAULT_PROBE_BUDGET,
              on_item: Callable[[str], None] | None = None) -> RemoteView:
        """changes 소비(B1~B4). is_complete=False.

        known_by_file_id: file_id → rel_path. `deleted`는 id만 오므로(실측) 경로를 알려면
        상태 DB의 역참조가 필요하다. 여기에 없는 id의 deleted는 **무시한다**(B3 — 동기화
        범위 밖이거나 애초에 모르던 객체다. 정상 동작이며 오류가 아니다).

        probe_file_ids: changes가 말해 주지 않는데 **이번 패스가 반드시 원격 상태를 알아야
        하는** 레코드의 file_id. 개별 메타 조회로 관측 목록에 넣는다(_probe_dirty).
        호출측이 무엇을 넣을지 정한다 — 미완료 레코드(I6)와 로컬에서 사라진 레코드가 그것이다.
        """
        known = dict(known_by_file_id or {})
        view = RemoteView(is_complete=False, cursor=cursor)
        bad_dirs: set[str] = set()
        seen: dict[str, str] = {}
        relisted: set[str] = set()
        last = cursor
        # '완전히 소비한 마지막 revision'까지의 커서. 질의에 fileId를 싣지 않으므로
        # (models.Cursor 참조) 커서는 revision 단위로만 끊을 수 있다 — 같은 revision에
        # 항목이 여럿인데 그 중간에서 끊고 revision을 물려 버리면 나머지 형제가 누락된다.
        safe = cursor

        for ci, at in self.drive.iter_changes(self.drive_id, cursor):
            view.changes_seen += 1
            if view.changes_seen > max_items:
                # 잘렸다는 사실을 숨기지 않는다 — 호출측은 부분 처리로 간주하고 이어받는다.
                # 경계 항목의 revision을 다 소비했는지 알 수 없으므로 **완전히 소비한
                # 마지막 revision까지만** 물린다. 다시 받는 것은 무해하지만(상태 기반
                # 판정이라 멱등) 빠뜨리는 것은 치명적이다(R11 계열의 조용한 누락).
                view.truncated = True
                if at.revision <= last.revision:
                    # 같은 revision 한가운데서 끊겼다 — 그 revision의 형제를 잃지 않도록
                    # 직전에 완전히 소비한 revision까지만 물린다.
                    last = safe
                # 경계 항목이 새 revision이면 직전 revision까지는 전부 소비된 것이라
                # last를 그대로 쓴다(불필요한 재수신을 만들지 않는다).
                self.log.warning("changes 항목이 %d건을 넘어 이번 패스를 여기서 끊습니다", max_items)
                break
            if at.revision > last.revision:
                safe = last          # 직전 revision은 전부 소비됐다
            last = at

            if ci.is_deleted:
                self._absorb_deleted(view, ci, known)
                continue

            rel = rel_from_remote(ci.full_path or "", self.prefix)
            if not rel:
                # 접두 밖이다. 원래 우리 것이던 파일이 밖으로 나갔다면 '범위 이탈'로 보고만 한다.
                # 삭제로 단정하지 않는다 — 사용자는 지운 적이 없다(규약_M2 I2).
                prev = known.get(ci.file_id or "")
                if prev:
                    view.moved_out_keys.add(path_key(prev))
                continue

            if on_item is not None:
                on_item(rel)
            entry = RemoteEntry.from_change(ci, rel)
            self._absorb_entry(view, entry, seen, bad_dirs)

            # B4: 폴더 updated → 하위 트리 재열람. 폴더 개명·이동 시 하위 파일에는
            # 이벤트가 오지 않으므로(R8 실측), 이걸 빠뜨리면 하위 전체가 '원격에서 사라진 것'
            # 처럼 보인다. 대량 오삭제로 가는 가장 짧은 길이다.
            if entry.is_dir and entry.file_id and entry.file_id not in relisted:
                relisted.add(entry.file_id)
                self._relist_subtree(view, entry, seen, bad_dirs, relisted, on_item=on_item)

        view.cursor = last
        self._probe_dirty(view, probe_file_ids, known, seen, bad_dirs, max_probes)
        return view

    # ------------------------------------------------------------------ 내부
    def _relist_subtree(self, view: RemoteView, folder: RemoteEntry, seen: dict[str, str],
                        bad_dirs: set[str], relisted: set[str] | None = None, *,
                        on_item: Callable[[str], None] | None = None) -> None:
        """폴더 하위 트리를 목록 API로 재열람해 자손 경로를 갱신한다(B4/R8).

        순회 중 만난 자손 폴더도 `relisted`에 넣는다. 넣지 않으면 changes에 상위와 하위
        폴더가 함께 들어왔을 때(초기 업로드 직후·트리 통째 이동) 같은 하위를 깊이만큼
        반복 열람해, '가벼운 델타'가 전체 순회보다 비싸진다.
        """
        base_path = join_remote("/" + self.prefix if self.prefix else "", folder.rel_path)
        n = 0
        try:
            for rf, full in self.drive.walk(self.drive_id, folder.file_id, base_path=base_path):
                rel = rel_from_remote(full, self.prefix)
                if not rel:
                    continue
                if relisted is not None and rf.is_dir and rf.id:
                    relisted.add(rf.id)          # 이 폴더는 방금 커버됐다
                n += 1
                if n > _MAX_SUBTREE_ITEMS:
                    view.truncated = True
                    self.log.warning("하위 재열람이 상한을 넘었습니다: %s", folder.rel_path)
                    break
                if on_item is not None:
                    on_item(rel)
                self._absorb(view, rf, rel, seen, bad_dirs)
        except DoorayApiError as exc:
            # 재열람 실패를 무시하면 그 하위가 '관측되지 않음'으로 남는다. 그것 자체는
            # 안전하지만(삭제로 읽히지 않는다) 뷰가 잘렸다는 사실은 반드시 남긴다.
            view.truncated = True
            self.log.warning("하위 재열람 실패(%s): %s", folder.rel_path, exc)
        view.subtrees_relisted += 1

    def _absorb(self, view: RemoteView, rf: RemoteFile, rel: str,
                seen: dict[str, str], bad_dirs: set[str]) -> None:
        self._absorb_entry(view, RemoteEntry.from_remote_file(rf, rel), seen, bad_dirs)

    def _absorb_entry(self, view: RemoteView, entry: RemoteEntry,
                      seen: dict[str, str], bad_dirs: set[str]) -> None:
        key = entry.rel_path_key
        rel = entry.rel_path

        prev = seen.get(key)
        if prev is not None and prev != rel:
            # 대소문자/정규화만 다른 이름은 Windows에 함께 저장할 수 없다.
            view.collisions.append(f"{rel}  (이미 {prev})")
            return
        seen[key] = rel

        if self.exclude and matches_any(rel, self.exclude):
            return

        parent_key = key.rpartition("/")[0]
        if parent_key and parent_key in bad_dirs:
            if entry.is_dir:
                bad_dirs.add(key)
            view.unsyncable.append((rel, "상위 폴더가 Windows에 저장 불가"))
            return
        issue = name_issue(entry.server_name or rel.rpartition("/")[2])
        if issue:
            if entry.is_dir:
                bad_dirs.add(key)
            view.unsyncable.append((rel, issue))
            return

        # 같은 키를 델타에서 두 번 보면 나중 것이 최신이다(항목은 revision 오름차순).
        view.entries[key] = entry
        view.deleted_keys.discard(key)
        view.moved_out_keys.discard(key)

    def _absorb_deleted(self, view: RemoteView, ci: ChangeItem,
                        known: dict[str, str]) -> None:
        """deleted 항목 반영. **미지 id는 무시한다**(B3 — 정상 동작)."""
        rel = known.get(ci.file_id or "")
        if not rel:
            return
        key = path_key(rel)
        view.deleted_keys.add(key)
        view.entries.pop(key, None)

    def _probe_dirty(self, view: RemoteView, probe_file_ids: Iterable[str],
                     known: dict[str, str], seen: dict[str, str],
                     bad_dirs: set[str], max_probes: int = DEFAULT_PROBE_BUDGET) -> None:
        """호출측이 지목한 항목만 원격 메타를 직접 확인한다(규약_M2 I6).

        델타는 '변경이 없으면 아무 말도 하지 않는' 뷰다. 그래서 changes가 언급하지 않는
        두 부류는 여기서 따로 확인해 주지 않으면 영원히 관측되지 않는다.

        1. 지난 실행에서 전송이 실패한 항목(sync_status != 'synced')
        2. **로컬에서 사라진 항목** — 로컬 삭제는 원격에 아무 사건도 만들지 않으므로
           changes에 나올 수가 없다. 이것을 빠뜨리면 삭제 전파를 켜도 사용자가
           'sync --full'을 직접 치기 전에는 아무 일도 일어나지 않는다(2026-08-10 사용자 보고).

        어느 쪽이든 비용은 **지목된 건수**에 비례하고 드라이브 크기와 무관하다.
        """
        for fid in probe_file_ids:
            if not fid or fid in view.probed_ids:
                continue
            rel_known = known.get(fid, "")
            if rel_known and view.observed(path_key(rel_known)):
                continue
            if len(view.probed_ids) >= max(0, max_probes):
                # init 직후처럼 미완료 레코드가 수천 건이면 개별 조회가 폭주한다.
                # 자르되 **숨기지 않는다** — 남은 건수는 호출측이 보고하고, 다음 실행이나
                # 'sync --full'에서 이어서 확인한다.
                view.probe_skipped += 1
                continue
            view.probed_ids.add(fid)
            try:
                rf = self.drive.get_file_meta(self.drive_id, fid)
            except DoorayApiError as exc:
                # 없어졌는지 권한이 없는지 구분할 수 없다. **삭제로 단정하지 않는다** —
                # 관측하지 않은 것으로 두면 이번 패스에서 아무 일도 일어나지 않는다(안전).
                self.log.info("원격 메타 확인 실패(삭제로 단정하지 않음) file_id=%s: %s", fid, exc)
                continue
            if not rf.id:
                continue
            if rf.parent_path:
                rel = rel_from_remote(rf.full_path, self.prefix)
            else:
                # 메타에 부모 경로가 없으면 위치를 알 수 없다. 루트에 있다고 **추정하지 않는다** —
                # 엉뚱한 경로로 흡수되면 그 자리의 다른 파일을 덮어쓸 계획이 만들어진다.
                # 아는 경로(base)가 있으면 그 자리의 최신 메타로만 쓴다.
                rel = rel_known
            if not rel:
                if rel_known:
                    view.moved_out_keys.add(path_key(rel_known))
                continue
            self._absorb(view, rf, rel, seen, bad_dirs)


def iter_known_by_file_id(records: Iterable) -> dict[str, str]:
    """FileRecord 목록에서 file_id → rel_path 역색인을 만든다(deleted 역참조용)."""
    out: dict[str, str] = {}
    for rec in records:
        fid = getattr(rec, "file_id", None)
        if fid:
            out.setdefault(str(fid), rec.rel_path)
    return out
