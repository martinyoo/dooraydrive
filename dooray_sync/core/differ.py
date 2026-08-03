"""3-way diff — 규약_M2 §3 / 구현계획서 §3.3 결정표 13케이스.

B(base=마지막 동기화 시점) × L(로컬 스냅샷) × R(원격 뷰)를 대조해 무엇을 할지 정한다.
**네트워크·파일시스템에 직접 접근하지 않는다** — 해시가 필요하면 주입된 콜백을 부른다.
13케이스를 표 그대로 단위 테스트할 수 있어야 하기 때문이고, 이 성질은 타협하지 않는다.

이 모듈이 지키는 세 가지 (규약_M2 I1~I3):

1. **관측하지 않은 것은 판단하지 않는다.** 델타 뷰에서 안 보이는 키는 '삭제'가 아니라
   '이번에 확인하지 않음'이다. 여기서 삭제를 유도하면 폴더 개명 한 번에 하위가 다 날아간다.
2. **삭제는 수정에게 진다.** 한쪽이 지우고 반대쪽이 고쳤으면 살아 있는 쪽을 복원한다(9·10).
3. **양쪽이 다르게 바뀌면 둘 다 남긴다.** 로컬을 충돌 사본으로 보존한 뒤 원격을 받는다(6).
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..core.scanner import LocalEntry
from ..store.db import FileRecord
from ..util.paths import path_key
from .remote import RemoteEntry, RemoteView

__all__ = [
    "Decision", "DiffStats", "diff", "conflict_copy_name",
    "KIND_MKDIR_LOCAL", "KIND_MKDIR_REMOTE", "KIND_UPLOAD_NEW", "KIND_UPLOAD_VERSION",
    "KIND_DOWNLOAD_NEW", "KIND_DOWNLOAD_UPDATE", "KIND_CONFLICT", "KIND_LOCAL_MOVE",
    "KIND_REMOTE_MOVE", "KIND_LOCAL_TRASH", "KIND_REMOTE_TRASH", "KIND_TOUCH_BASE",
    "KIND_FORGET", "KIND_REPORT", "KIND_PROTECT", "KIND_UNSYNCABLE",
]

KIND_MKDIR_LOCAL = "MKDIR_LOCAL"
KIND_MKDIR_REMOTE = "MKDIR_REMOTE"
KIND_UPLOAD_NEW = "UPLOAD_NEW"
KIND_UPLOAD_VERSION = "UPLOAD_VERSION"
KIND_DOWNLOAD_NEW = "DOWNLOAD_NEW"
KIND_DOWNLOAD_UPDATE = "DOWNLOAD_UPDATE"
KIND_CONFLICT = "CONFLICT"
KIND_LOCAL_MOVE = "LOCAL_MOVE"
KIND_REMOTE_MOVE = "REMOTE_MOVE"
KIND_LOCAL_TRASH = "LOCAL_TRASH"
KIND_REMOTE_TRASH = "REMOTE_TRASH"
KIND_TOUCH_BASE = "TOUCH_BASE"
KIND_FORGET = "FORGET"          # 양쪽 다 없어진 항목 — DB 레코드만 정리
KIND_REPORT = "REPORT"
KIND_PROTECT = "PROTECT"
KIND_UNSYNCABLE = "UNSYNCABLE"

# 실행을 동반하지 않는 판정(계획 표에서 '보고' 영역으로 묶인다)
PASSIVE_KINDS = frozenset({KIND_REPORT, KIND_PROTECT, KIND_UNSYNCABLE})


@dataclass(frozen=True)
class Decision:
    case: int                    # 결정표 번호 1~13 (표에 없는 정리 동작은 0)
    kind: str
    rel_path: str
    key: str
    is_dir: bool = False
    local: LocalEntry | None = None
    remote: RemoteEntry | None = None
    base: FileRecord | None = None
    new_rel_path: str = ""       # MOVE 계열의 목적지
    reason: str = ""

    @property
    def size(self) -> int | None:
        if self.remote is not None and self.remote.size is not None:
            return self.remote.size
        return None if self.local is None else self.local.size


@dataclass
class DiffStats:
    unchanged: int = 0
    skipped_unobserved: int = 0
    skipped_local_unobserved: int = 0
    md5_probes: int = 0
    md5_probe_skipped: int = 0      # 예산 초과로 확인 못 함 — 반드시 보고할 것
    hash_failures: list[tuple[str, str]] = field(default_factory=list)
    moves_local: int = 0
    moves_remote: int = 0


HashLocal = Callable[[LocalEntry], LocalEntry]
Md5Probe = Callable[[RemoteEntry], "str | None"]


def conflict_copy_name(rel_path: str, when: _dt.datetime) -> str:
    """'a/문서.docx' → 'a/문서 (충돌 2026-08-02 1530).docx' (구현계획서 §3.3 6번)."""
    head, _, name = str(rel_path).rpartition("/")
    stem, dot, ext = name.rpartition(".")
    tag = f" (충돌 {when.strftime('%Y-%m-%d %H%M')})"
    new_name = (stem + tag + dot + ext) if dot else (name + tag)
    return f"{head}/{new_name}" if head else new_name


# --------------------------------------------------------------------------- 판정 보조
def _has_baseline(rec: FileRecord | None) -> bool:
    """base가 '마지막 동기화 시점의 로컬 내용'을 실제로 알고 있는가."""
    return bool(rec is not None and not rec.is_dir and rec.local_md5)


def _meta_same(rec: FileRecord, entry: LocalEntry) -> bool:
    return (rec.local_mtime_ns is not None and rec.local_size is not None
            and entry.mtime_ns is not None and entry.size is not None
            and int(rec.local_mtime_ns) == entry.mtime_ns
            and int(rec.local_size) == entry.size)


def _remote_changed(rec: FileRecord, r: RemoteEntry) -> bool:
    """원격이 base 이후 바뀌었는가.

    **version만 보면 안 된다** — 이름 변경은 version을 올리지 않는다(R13 실측).
    같은 키(=같은 경로)에서의 개명은 있을 수 없으므로 여기서는 version·size·md5를 본다.
    경로가 바뀌는 개명/이동은 file_id 역색인으로 따로 잡는다(결정표 12).
    """
    if rec.remote_version is not None and r.version is not None:
        if int(rec.remote_version) != int(r.version):
            return True
    elif rec.remote_version is None and r.version is not None:
        return True
    if rec.remote_size is not None and r.size is not None and int(rec.remote_size) != int(r.size):
        return True
    if rec.remote_md5 and r.md5 and rec.remote_md5.lower() != r.md5.lower():
        return True
    return False


class _Ctx:
    """diff 1회분의 가변 상태(해시 캐시·예산). diff 밖으로 새지 않는다."""

    def __init__(self, hash_local: HashLocal | None, md5_probe: Md5Probe | None,
                 budget: int, stats: DiffStats) -> None:
        self.hash_local = hash_local
        self.md5_probe = md5_probe
        self.budget = max(0, int(budget))
        self.stats = stats
        self._remote_md5: dict[str, str | None] = {}

    def local_md5(self, entry: LocalEntry) -> str | None:
        """로컬 MD5. **계산 결과를 entry에 되쓴다.**

        `LocalScanner.fill_md5`는 `dataclasses.replace`로 새 객체를 돌려주므로, 반환값만
        받아 쓰면 Decision이 들고 가는 원본 entry의 md5는 None으로 남는다. 그러면
        executor가 base에 `local_md5=NULL`을 기록하고, 다음 실행의 `_has_baseline`이
        실패해 그 파일은 영원히 PROTECT만 된다 — 업로드가 1회용이 된다.
        """
        if entry.md5:
            return entry.md5
        if entry.is_dir or self.hash_local is None:
            return None
        try:
            got = self.hash_local(entry).md5
            if got:
                entry.md5 = got      # LocalEntry는 frozen이 아니다 — 캐시를 원본에 채운다
            return got
        except OSError as exc:
            # 오피스·백신이 잠근 파일이 여기로 온다. 조용히 빠지면 '올리지 못했는데 성공'이
            # 되므로 반드시 집계한다(M1에서 실제로 났던 결함).
            self.stats.hash_failures.append(
                (entry.rel_path, f"해시 실패 — {type(exc).__name__}: {exc}"))
            return None

    def remote_md5(self, r: RemoteEntry) -> str | None:
        """원격 MD5. 목록 API 응답에는 hash가 없어(실측) 받아서 계산해야 할 수 있다."""
        if r.md5:
            return r.md5
        if r.file_id in self._remote_md5:
            return self._remote_md5[r.file_id]
        if self.md5_probe is None:
            return None
        if self.stats.md5_probes >= self.budget:
            self.stats.md5_probe_skipped += 1
            return None
        self.stats.md5_probes += 1
        try:
            got = self.md5_probe(r)
        except Exception:  # noqa: BLE001 — 확인 실패는 '모름'이지 오류가 아니다
            got = None
        self._remote_md5[r.file_id] = got
        return got

    def same_content(self, entry: LocalEntry, r: RemoteEntry) -> bool | None:
        """내용이 같은가. 판단할 수 없으면 None(= 모름 → 안전한 쪽으로 처리)."""
        if entry.size is not None and r.size is not None and entry.size != r.size:
            return False        # 크기가 다르면 받아 볼 필요도 없다
        lm = self.local_md5(entry)
        if not lm:
            return None
        rm = self.remote_md5(r)
        if not rm:
            return None
        return lm.lower() == rm.lower()


# --------------------------------------------------------------------------- 본체
def _unknown_locally(key: str, unobserved: frozenset[str], all_unknown: bool) -> bool:
    """로컬 스캔이 이 키의 상태를 실제로 확인했는가(I1의 로컬 축).

    스캔은 접근 거부·백신 잠금·나열 중단 시 **그 항목만 건너뛰고 계속 진행**한다.
    건너뛴 것을 '없다'로 읽으면 일시적 IO 오류가 곧바로 원격 삭제로 전파된다.
    """
    if all_unknown:
        return True
    if not unobserved:
        return False
    if key in unobserved:
        return True
    return any(key.startswith(u + "/") for u in unobserved)


def diff(*, base: dict[str, FileRecord], local: dict[str, LocalEntry], remote: RemoteView,
         propagate_deletes: bool = False, hash_local: HashLocal | None = None,
         md5_probe: Md5Probe | None = None, md5_probe_budget: int = 200,
         local_unobserved: Sequence[str] = ()) -> tuple[list[Decision], DiffStats]:
    """B/L/R을 대조해 Decision 목록을 낸다. 키는 전부 rel_path_key.

    `local_unobserved`는 스캔이 건너뛴 경로들이다(LocalScanner.skipped). 그 경로와 하위는
    '로컬에 없음'이 아니라 '모름'으로 취급해 삭제 판정을 내지 않는다 — 원격 뷰의
    `observed()`와 대칭이며, I1을 로컬 축에도 적용하는 부분이다.
    """
    stats = DiffStats()
    ctx = _Ctx(hash_local, md5_probe, md5_probe_budget, stats)
    out: list[Decision] = []

    raw_unobs = [str(u or "") for u in (local_unobserved or ())]
    # 루트 자체를 못 읽었으면 로컬 전체가 미확인이다 — 어떤 삭제도 내지 않는다.
    all_unknown = any(u in ("", "(루트)") for u in raw_unobs)
    unobserved = frozenset(path_key(u) for u in raw_unobs if u and u != "(루트)")

    remote_by_id = remote.by_file_id()
    # 원격 이동(12): base의 file_id가 원격에서 **다른 경로로** 관측된 경우.
    moved_remote: dict[str, RemoteEntry] = {}
    for key, rec in base.items():
        if not rec.file_id:
            continue
        r = remote_by_id.get(rec.file_id)
        if r is not None and r.rel_path_key != key:
            moved_remote[key] = r

    # 원격 이동의 목적지 키는 '원격 신규'가 아니다 — 아래 본 루프에서 건너뛴다.
    move_targets = {r.rel_path_key for r in moved_remote.values()}

    # 로컬 이동(11): base에 있던 파일이 로컬에서 사라지고, 같은 (size, md5)를 가진
    # base 없는 로컬 신규가 **정확히 1건**일 때만 승격한다. 후보가 여럿이면 승격하지
    # 않는다 — 오판이 곧 원격 파일의 엉뚱한 이동이 되기 때문이다.
    moved_local = _detect_local_moves(base, local, remote, ctx)
    local_move_targets = {v[0] for v in moved_local.values()}

    keys = set(base) | set(local) | set(remote.entries) | set(remote.deleted_keys)
    keys |= set(remote.moved_out_keys)

    for key in sorted(keys):
        rec = base.get(key)
        entry = local.get(key)
        r = remote.entries.get(key)

        if rec is not None and rec.sync_status == "ignored":
            stats.unchanged += 1
            continue

        # --- 이동 먼저 (결정표 11·12·13: "이동을 먼저 반영한 뒤 나머지를 재적용")
        if key in moved_remote:
            out.extend(_remote_move(key, rec, entry, moved_remote[key], ctx, stats))
            continue
        if key in moved_local:
            new_key, new_entry = moved_local[key]
            stats.moves_remote += 1
            out.append(Decision(
                case=11, kind=KIND_REMOTE_MOVE, rel_path=rec.rel_path, key=key,
                is_dir=False, local=new_entry, base=rec, new_rel_path=new_entry.rel_path,
                reason=f"로컬에서 이동/개명됨 → 원격도 옮김: {new_entry.rel_path}"))
            continue
        if key in local_move_targets or key in move_targets:
            continue            # 이동의 목적지 — 위에서 이미 처리했다

        r_observed = remote.observed(key)
        r_deleted = key in remote.deleted_keys
        r_moved_out = key in remote.moved_out_keys and not r_deleted

        if r_moved_out:
            out.append(Decision(
                case=0, kind=KIND_REPORT, rel_path=(rec.rel_path if rec else key), key=key,
                is_dir=bool(rec and rec.is_dir), base=rec, local=entry,
                reason="원격에서 동기화 범위 밖으로 이동됨 — 로컬은 그대로 둡니다"))
            continue

        # 원격이 완전 뷰인데 없으면 '삭제', 델타에서 안 보이면 '모름'(I1)
        if r is None and not r_deleted and not remote.is_complete:
            if rec is None and entry is None:
                continue
            if rec is not None and entry is not None:
                # 로컬 변경만 판정하면 되는 상황 — 원격은 base와 같다고 본다
                stats.skipped_unobserved += 1
            elif rec is not None and entry is None:
                stats.skipped_unobserved += 1

        # 로컬이 안 보이는데 스캔이 그 경로를 확인하지 못했다면 '삭제'가 아니라 '모름'이다.
        # 백신 잠금·권한 오류로 디렉터리 나열이 중단된 것을 삭제로 읽으면, 일시적 IO 오류가
        # 그대로 원격 삭제로 전파된다(스캐너 주석이 호출측에 요구하는 확인이 바로 이것이다).
        if entry is None and rec is not None and _unknown_locally(key, unobserved, all_unknown):
            stats.skipped_local_unobserved += 1
            out.append(Decision(
                case=0, kind=KIND_REPORT, rel_path=rec.rel_path, key=key,
                is_dir=bool(rec.is_dir), base=rec, remote=r,
                reason="로컬 스캔이 이 경로를 확인하지 못했습니다 — 삭제로 판단하지 않습니다"))
            continue

        out.extend(_decide_one(key, rec, entry, r, r_deleted, r_observed,
                               propagate_deletes, ctx, stats, remote.is_complete))

    for rel, why in remote.unsyncable:
        out.append(Decision(case=0, kind=KIND_UNSYNCABLE, rel_path=rel,
                            key=path_key(rel), reason=why))
    return out, stats


def _remote_move(key: str, rec: FileRecord | None, entry: LocalEntry | None,
                 r: RemoteEntry, ctx: _Ctx, stats: DiffStats) -> list[Decision]:
    """결정표 12: 원격에서 이동/개명됨 → 로컬도 옮긴다.

    로컬이 그 사이에 수정됐으면 옮기지 않는다 — 옮긴 뒤 원격 내용으로 덮으면 편집이
    사라진다. 그 경우 로컬은 그대로 두고(PROTECT) 새 경로는 신규 수신으로 처리한다.
    """
    rel = rec.rel_path if rec else key
    if entry is None:
        # 로컬에 원본이 없다 — 옮길 것이 없으니 새 경로로 새로 받는다.
        # 옛 키의 base 레코드는 **반드시 함께 정리한다.** 남겨 두면 그 키는 이후 어떤
        # 결정도 받지 못하는 유령이 되고(원격은 새 경로에서만 관측된다), 새 경로에는
        # file_id가 같은 레코드가 하나 더 생겨 이후 로컬 편집이 영원히 올라가지 않는다.
        out = [Decision(case=2, kind=(KIND_MKDIR_LOCAL if r.is_dir else KIND_DOWNLOAD_NEW),
                        rel_path=r.rel_path, key=r.rel_path_key, is_dir=r.is_dir,
                        remote=r, base=rec,
                        reason=f"원격에서 이동됨(로컬 원본 없음): {rel} → {r.rel_path}")]
        if rec is not None:
            out.append(Decision(case=0, kind=KIND_FORGET, rel_path=rel, key=key,
                                is_dir=bool(rec.is_dir), base=rec,
                                reason=f"원격에서 {r.rel_path}로 이동됨 — 옛 경로 기록 정리"))
        return out

    # 기준선이 없으면 로컬이 수정됐는지 알 수 없다 — 옮긴 뒤 원격본으로 덮으면 편집이
    # 사본도 없이 사라진다. 이동이 끼었을 뿐 "어느 쪽이 최신인지 모른다"는 사실은 같으므로
    # _decide_one의 '기준선 없음 → PROTECT'와 같은 판단이어야 한다.
    if rec is not None and not rec.is_dir and not _has_baseline(rec):
        return [
            Decision(case=13, kind=KIND_PROTECT, rel_path=rel, key=key, local=entry,
                     base=rec, remote=r,
                     reason="원격은 이동, 로컬 기준선 없음 — 로컬을 그대로 두고 "
                            "'dsync reconcile'로 대조하세요"),
            Decision(case=2, kind=KIND_DOWNLOAD_NEW, rel_path=r.rel_path,
                     key=r.rel_path_key, remote=r,
                     reason=f"원격 이동본을 새 경로로 받습니다({rel} → {r.rel_path})"),
        ]

    if rec is not None and not rec.is_dir and _has_baseline(rec) and not _meta_same(rec, entry):
        lm = ctx.local_md5(entry)
        if lm and lm.lower() != (rec.local_md5 or "").lower():
            return [
                Decision(case=13, kind=KIND_PROTECT, rel_path=rel, key=key, local=entry,
                         base=rec, remote=r,
                         reason="원격은 이동, 로컬은 수정 — 로컬을 그대로 두고 보고만 합니다"),
                Decision(case=2, kind=KIND_DOWNLOAD_NEW, rel_path=r.rel_path,
                         key=r.rel_path_key, remote=r,
                         reason=f"원격 이동본을 새 경로로 받습니다({rel} → {r.rel_path})"),
            ]

    stats.moves_local += 1
    out = [Decision(case=12, kind=KIND_LOCAL_MOVE, rel_path=rel, key=key,
                    is_dir=r.is_dir, local=entry, remote=r, base=rec,
                    new_rel_path=r.rel_path,
                    reason=f"원격에서 이동/개명됨 → 로컬도 옮김: {r.rel_path}")]

    # 결정표 13: 이동 **먼저** 반영한 뒤 나머지를 재적용한다.
    # 옮기기만 하고 끝내면 executor가 새 remote_version을 base에 기록하므로, 같은 작업에서
    # 함께 일어난 원격 내용 수정이 '이미 받은 것'으로 둔갑해 영원히 로컬에 도달하지 않는다.
    # (웹 UI에서 이름 바꾸고 바로 편집하는 것은 아주 흔한 동작이다.)
    if rec is not None and not r.is_dir and _remote_changed(rec, r):
        out.append(Decision(case=13, kind=KIND_DOWNLOAD_UPDATE, rel_path=r.rel_path,
                            key=r.rel_path_key, local=entry, remote=r, base=rec,
                            reason=f"이동과 함께 원격 내용도 수정됨(version "
                                   f"{rec.remote_version} → {r.version}) — 옮긴 뒤 받습니다"))
    return out


def _detect_local_moves(base: dict[str, FileRecord], local: dict[str, LocalEntry],
                        remote: RemoteView, ctx: _Ctx,
                        ) -> dict[str, tuple[str, LocalEntry]]:
    """'사라진 base' × 'base 없는 로컬 신규'를 (size, md5)로 짝지어 이동으로 승격한다.

    승격 조건을 엄격히 둔다 — 잘못 짝지으면 원격 파일을 엉뚱한 곳으로 옮기는 결과가 된다.
      · 양쪽 모두 후보가 정확히 1건
      · 크기가 같고 MD5가 같음
      · 원격이 그 파일을 삭제했다고 관측하지 않았음(그 경우는 결정표 10이 우선)
    """
    gone = [
        (k, rec) for k, rec in base.items()
        if not rec.is_dir and rec.file_id and _has_baseline(rec)
        and k not in local and k not in remote.deleted_keys
    ]
    fresh = [
        (k, e) for k, e in local.items()
        if not e.is_dir and k not in base and k not in remote.entries
    ]
    if not gone or not fresh:
        return {}

    by_size: dict[int, list[tuple[str, LocalEntry]]] = {}
    for k, e in fresh:
        if e.size is not None:
            by_size.setdefault(int(e.size), []).append((k, e))

    out: dict[str, tuple[str, LocalEntry]] = {}
    taken: set[str] = set()
    for k, rec in gone:
        if rec.local_size is None:
            continue
        cands = [c for c in by_size.get(int(rec.local_size), []) if c[0] not in taken]
        if len(cands) != 1:
            continue                       # 후보가 0건이거나 여럿이면 승격하지 않는다
        nk, entry = cands[0]
        # 같은 크기의 다른 base가 이 후보를 노리고 있으면 짝짓기가 모호하다 — 포기.
        rivals = [1 for _k2, r2 in gone
                  if _k2 != k and r2.local_size is not None
                  and int(r2.local_size) == int(rec.local_size)]
        if rivals:
            continue
        lm = ctx.local_md5(entry)
        if not lm or lm.lower() != (rec.local_md5 or "").lower():
            continue
        taken.add(nk)
        out[k] = (nk, entry)
    return out


def _decide_one(key: str, rec: FileRecord | None, entry: LocalEntry | None,
                r: RemoteEntry | None, r_deleted: bool, r_observed: bool,
                propagate_deletes: bool, ctx: _Ctx, stats: DiffStats,
                remote_complete: bool) -> list[Decision]:
    """키 하나에 대한 결정표 적용."""
    rel = (entry.rel_path if entry is not None
           else r.rel_path if r is not None
           else rec.rel_path if rec is not None else key)
    is_dir = bool((entry and entry.is_dir) or (r and r.is_dir) or (rec and rec.is_dir))

    # 원격이 '완전 뷰에서 부재' = 삭제로 본다. 델타에서 안 보이는 것은 삭제가 아니다(I1).
    r_absent = r_deleted or (r is None and remote_complete)

    # ---------- base 없음 (1·2·3) ----------
    if rec is None or not rec.file_id:
        if entry is not None and (r is None or r_deleted):
            if is_dir:
                return [Decision(case=1, kind=KIND_MKDIR_REMOTE, rel_path=rel, key=key,
                                 is_dir=True, local=entry, reason="로컬 신규 폴더")]
            return [Decision(case=1, kind=KIND_UPLOAD_NEW, rel_path=rel, key=key,
                             local=entry, base=rec, reason="로컬 신규")]
        if entry is None and r is not None:
            if r.is_dir:
                return [Decision(case=2, kind=KIND_MKDIR_LOCAL, rel_path=rel, key=key,
                                 is_dir=True, remote=r, reason="원격 신규 폴더")]
            return [Decision(case=2, kind=KIND_DOWNLOAD_NEW, rel_path=rel, key=key,
                             remote=r, base=rec, reason="원격 신규")]
        if entry is not None and r is not None:
            # 3번: 양쪽 신규. 같은 내용이면 병합(기록만), 다르면 양쪽 보존.
            if entry.is_dir and r.is_dir:
                return [Decision(case=3, kind=KIND_TOUCH_BASE, rel_path=rel, key=key,
                                 is_dir=True, local=entry, remote=r,
                                 reason="양쪽에 같은 폴더가 있음 — 기록만 맞춥니다")]
            if entry.is_dir != r.is_dir:
                return [Decision(case=3, kind=KIND_PROTECT, rel_path=rel, key=key,
                                 local=entry, remote=r,
                                 reason="한쪽은 폴더, 한쪽은 파일 — 건드리지 않습니다")]
            same = ctx.same_content(entry, r)
            if same is True:
                return [Decision(case=3, kind=KIND_TOUCH_BASE, rel_path=rel, key=key,
                                 local=entry, remote=r,
                                 reason="양쪽에 같은 내용이 있음 — 전송 없이 기준선만 기록")]
            return [Decision(case=3, kind=KIND_CONFLICT, rel_path=rel, key=key,
                             local=entry, remote=r,
                             reason=("양쪽에 서로 다른 파일이 있음" if same is False
                                     else "양쪽에 파일이 있으나 내용을 대조하지 못함"))]
        return []       # base도 로컬도 원격도 없음 — 있을 수 없지만 무해하게 무시

    # ---------- 폴더 ----------
    if rec.is_dir or is_dir:
        return _decide_dir(key, rel, rec, entry, r, r_absent, r_observed,
                           propagate_deletes, remote_complete)

    # ---------- base 있음 (4~10) ----------
    local_present = entry is not None
    if not _has_baseline(rec) and local_present:
        # 기준선이 없으면 어느 쪽이 최신인지 알 수 없다. M1 push의 '보류'와 같은 처리.
        return [Decision(case=0, kind=KIND_PROTECT, rel_path=rel, key=key, local=entry,
                         remote=r, base=rec,
                         reason="로컬 기준선 없음 — 'dsync reconcile'로 원격과 대조하세요")]

    local_changed = False
    if local_present:
        if not _meta_same(rec, entry):
            lm = ctx.local_md5(entry)
            if lm is None:
                return [Decision(case=0, kind=KIND_PROTECT, rel_path=rel, key=key,
                                 local=entry, remote=r, base=rec,
                                 reason="로컬 내용을 읽지 못해 판정할 수 없습니다")]
            local_changed = lm.lower() != (rec.local_md5 or "").lower()

    remote_changed = bool(r is not None and _remote_changed(rec, r))

    # 9·10: 삭제 vs 수정 — **보존이 이긴다**(I2)
    # 여기서 쓰는 것은 r_deleted가 아니라 r_absent다. 전체 뷰에서 목록에 없는 것도
    # '원격에서 사라짐'이다 — 톰스톤(changes deleted)이 올 때만 삭제로 보면 전체
    # 재조정이 원격 삭제를 영원히 못 잡는다.
    if not local_present and r_absent:
        return [Decision(case=0, kind=KIND_FORGET, rel_path=rel, key=key, base=rec,
                         reason="양쪽에서 사라짐 — 기록만 정리합니다")]
    if not local_present and remote_changed:
        return [Decision(case=9, kind=KIND_DOWNLOAD_NEW, rel_path=rel, key=key, remote=r,
                         base=rec,
                         reason="로컬 삭제 vs 원격 수정 — 보존 우선으로 원격본을 되받습니다")]
    if local_changed and r_absent:
        return [Decision(case=10, kind=KIND_UPLOAD_NEW, rel_path=rel, key=key, local=entry,
                         base=rec,
                         reason="로컬 수정 vs 원격 삭제 — 보존 우선으로 다시 올립니다")]

    # 로컬에 없는 파일에는 서로 다른 두 상황이 섞여 있다(폴더판 _decide_dir와 같은 구분).
    #   (a) 아직 한 번도 받은 적 없음(init 직후) → 받아야 한다
    #   (b) 받았었는데 사용자가 지웠음 → 결정표 7(로컬 삭제)
    # 구분하지 않고 전부 (b)로 보면, **새 PC에서 init 직후 첫 sync가 원격 파일을 전부
    # 휴지통으로 보낸다**(삭제 전파가 켜져 있을 때). init은 원격 상태만 기록하고 파일은
    # 받지 않으므로 이 상태가 정상적으로 발생한다.
    # 판별: 기준선(local_md5)이 있으면 한 번은 로컬에 실현됐던 것이다.
    if not local_present and r is not None and not _has_baseline(rec):
        return [Decision(case=2, kind=KIND_DOWNLOAD_NEW, rel_path=rel, key=key,
                         remote=r, base=rec,
                         reason="원격에 있고 로컬에 아직 받지 않음")]

    # 7: 로컬 삭제 · 원격 불변
    if not local_present:
        if not r_absent and not r_observed:
            return [Decision(case=7, kind=KIND_REPORT, rel_path=rel, key=key, base=rec,
                             reason="로컬에서 사라짐 (원격 상태 미확인 — 'dsync sync --full'로 확인)")]
        if not propagate_deletes:
            return [Decision(case=7, kind=KIND_REPORT, rel_path=rel, key=key, base=rec,
                             remote=r,
                             reason="로컬에서 사라짐 — 삭제 전파가 꺼져 있어 보고만 합니다")]
        return [Decision(case=7, kind=KIND_REMOTE_TRASH, rel_path=rel, key=key, base=rec,
                         remote=r, reason="로컬에서 삭제됨 → 원격을 휴지통으로")]

    # 8: 원격 삭제 · 로컬 불변
    if r_absent and not local_changed:
        if not propagate_deletes:
            return [Decision(case=8, kind=KIND_REPORT, rel_path=rel, key=key, base=rec,
                             local=entry,
                             reason="원격에서 삭제됨 — 삭제 전파가 꺼져 있어 보고만 합니다")]
        return [Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path=rel, key=key, base=rec,
                         local=entry, reason="원격에서 삭제됨 → 로컬을 휴지통으로")]

    # 4·5·6
    if local_changed and remote_changed:
        same = ctx.same_content(entry, r) if r is not None else None
        if same is True:
            return [Decision(case=6, kind=KIND_TOUCH_BASE, rel_path=rel, key=key,
                             local=entry, remote=r, base=rec,
                             reason="양쪽이 바뀌었으나 내용이 같음 — 기준선만 갱신(가짜 충돌)")]
        return [Decision(case=6, kind=KIND_CONFLICT, rel_path=rel, key=key, local=entry,
                         remote=r, base=rec,
                         reason=("양쪽에서 수정됨" if same is False
                                 else "양쪽에서 수정됨(내용 대조 실패)"))]
    if local_changed:
        return [Decision(case=4, kind=KIND_UPLOAD_VERSION, rel_path=rel, key=key,
                         local=entry, remote=r, base=rec, reason="로컬에서 수정됨")]
    if remote_changed:
        return [Decision(case=5, kind=KIND_DOWNLOAD_UPDATE, rel_path=rel, key=key,
                         local=entry, remote=r, base=rec,
                         reason=f"원격에서 수정됨(version {rec.remote_version} → {r.version})")]

    stats.unchanged += 1
    return []


def _decide_dir(key: str, rel: str, rec: FileRecord, entry: LocalEntry | None,
                r: RemoteEntry | None, r_absent: bool, r_observed: bool,
                propagate_deletes: bool, remote_complete: bool) -> list[Decision]:
    """폴더는 내용이 없으므로 존재/부재만 본다. 삭제는 하위에 재귀 적용된다(C5)."""
    if entry is not None and (r is not None or not r_observed):
        return []                                  # 양쪽에 있음 — 할 일 없음
    if entry is None and r is not None:
        # 로컬에 없는 원격 폴더에는 두 가지 서로 다른 상황이 섞여 있다.
        #   (a) 아직 한 번도 만든 적 없음(init 직후) → 만들어야 한다
        #   (b) 만들었었는데 사용자가 지웠음 → 결정표 7(로컬 삭제)로 가야 한다
        # 구분하지 않고 항상 (a)로 처리하면 사용자가 지운 폴더가 매 실행 조용히 되살아나고,
        # 반대로 항상 (b)로 처리하면 init 직후 첫 sync가 원격 폴더를 전부 휴지통으로 보낸다.
        # 폴더는 내용 해시가 없으므로 '로컬에 실현된 적이 있는가'를 sync_status로 판별한다
        # (executor가 MKDIR_LOCAL 성공 시에만 'synced'로 기록한다).
        if rec.sync_status != "synced":
            return [Decision(case=2, kind=KIND_MKDIR_LOCAL, rel_path=rel, key=key,
                             is_dir=True, remote=r, base=rec,
                             reason="원격 폴더 — 로컬에 생성")]
        if not propagate_deletes:
            return [Decision(case=7, kind=KIND_REPORT, rel_path=rel, key=key, is_dir=True,
                             base=rec, remote=r,
                             reason="로컬에서 폴더가 사라짐 — 삭제 전파가 꺼져 있어 보고만 합니다"
                                    " (다시 만들지 않습니다)")]
        return [Decision(case=7, kind=KIND_REMOTE_TRASH, rel_path=rel, key=key, is_dir=True,
                         base=rec, remote=r,
                         reason="로컬에서 폴더가 삭제됨 → 원격을 휴지통으로")]
    if entry is not None and r_absent:
        if not propagate_deletes:
            return [Decision(case=8, kind=KIND_REPORT, rel_path=rel, key=key, is_dir=True,
                             base=rec, local=entry,
                             reason="원격에서 폴더가 사라짐 — 삭제 전파가 꺼져 있어 보고만 합니다")]
        return [Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path=rel, key=key, is_dir=True,
                         base=rec, local=entry,
                         reason="원격에서 폴더가 삭제됨 → 로컬 폴더를 휴지통으로")]
    if entry is None and r_absent:
        return [Decision(case=0, kind=KIND_FORGET, rel_path=rel, key=key, is_dir=True,
                         base=rec, reason="양쪽에서 폴더가 사라짐 — 기록만 정리합니다")]
    # 로컬에만 없다(원격에는 있다). 삭제 전파 여부에 따라 원격 휴지통 또는 보고.
    if not r_observed:
        return [Decision(case=7, kind=KIND_REPORT, rel_path=rel, key=key, is_dir=True,
                         base=rec, reason="로컬에서 폴더가 사라짐(원격 상태 미확인)")]
    if not propagate_deletes:
        return [Decision(case=7, kind=KIND_REPORT, rel_path=rel, key=key, is_dir=True,
                         base=rec, reason="로컬에서 폴더가 사라짐 — 보고만 합니다")]
    return [Decision(case=7, kind=KIND_REMOTE_TRASH, rel_path=rel, key=key, is_dir=True,
                     base=rec, remote=r, reason="로컬에서 폴더가 삭제됨 → 원격을 휴지통으로")]
