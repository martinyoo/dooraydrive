"""실행 계획 — 규약_M2 §4.

Decision(무엇을 해야 하는가)을 Action(어떤 순서로 실행하는가)으로 바꾸고, 실행 **전에**
안전 게이트를 건다. differ와 마찬가지로 **순수 함수**다 — 네트워크·파일시스템에 닿지 않는다.

여기서 막아야 하는 사고는 두 가지다.

- **대량 오삭제**: 삭제 건수가 임계를 넘으면 아무것도 실행하지 않고 중단한다(C5).
  반쯤 지우고 멈추는 것이 가장 나쁜 결과라, 부분 실행을 허용하지 않는다.
- **순서 사고**: 폴더가 없는데 파일을 내려받거나, 이동 전 경로에 쓰거나, 이미 휴지통에
  들어간 폴더의 자식을 또 지우려 하는 것.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import Profile
from .differ import (
    PASSIVE_KINDS,
    KIND_CONFLICT,
    KIND_DOWNLOAD_NEW,
    KIND_DOWNLOAD_UPDATE,
    KIND_FORGET,
    KIND_LOCAL_MOVE,
    KIND_LOCAL_TRASH,
    KIND_MKDIR_LOCAL,
    KIND_MKDIR_REMOTE,
    KIND_PROTECT,
    KIND_REMOTE_MOVE,
    KIND_REMOTE_TRASH,
    KIND_REPORT,
    KIND_TOUCH_BASE,
    KIND_UNSYNCABLE,
    KIND_UPLOAD_NEW,
    KIND_UPLOAD_VERSION,
    Decision,
)

__all__ = ["Action", "Plan", "BulkDeleteAbort", "plan", "ACTION_LABEL"]

# 계획 표에 쓰는 한국어 라벨
ACTION_LABEL = {
    KIND_MKDIR_REMOTE: "원격폴더생성",
    KIND_MKDIR_LOCAL: "로컬폴더생성",
    KIND_UPLOAD_NEW: "신규업로드",
    KIND_UPLOAD_VERSION: "새버전업로드",
    KIND_DOWNLOAD_NEW: "신규받기",
    KIND_DOWNLOAD_UPDATE: "갱신받기",
    KIND_CONFLICT: "충돌보존",
    KIND_LOCAL_MOVE: "로컬이동",
    KIND_REMOTE_MOVE: "원격이동",
    KIND_LOCAL_TRASH: "로컬휴지통",
    KIND_REMOTE_TRASH: "원격휴지통",
    KIND_TOUCH_BASE: "기록갱신",
    KIND_FORGET: "기록정리",
}

# 비율 기반 대량삭제 임계를 적용하기 시작하는 최소 기준선 크기.
# 이보다 작으면 비율이 한두 건에 걸려 정상 삭제까지 막는다(경보 피로 → 게이트 무력화).
RATIO_MIN_BASE = 20

TRASH_KINDS = (KIND_LOCAL_TRASH, KIND_REMOTE_TRASH)
MOVE_KINDS = (KIND_LOCAL_MOVE, KIND_REMOTE_MOVE)
MKDIR_KINDS = (KIND_MKDIR_REMOTE, KIND_MKDIR_LOCAL)

# 실행 순서 그룹. 낮을수록 먼저.
_ORDER = {
    KIND_MKDIR_REMOTE: 0,
    KIND_MKDIR_LOCAL: 0,
    KIND_LOCAL_MOVE: 1,
    KIND_REMOTE_MOVE: 1,
    KIND_CONFLICT: 2,
    KIND_DOWNLOAD_NEW: 2,
    KIND_DOWNLOAD_UPDATE: 2,
    KIND_UPLOAD_NEW: 2,
    KIND_UPLOAD_VERSION: 2,
    KIND_TOUCH_BASE: 3,
    KIND_FORGET: 3,
    KIND_LOCAL_TRASH: 4,
    KIND_REMOTE_TRASH: 4,
}


class BulkDeleteAbort(RuntimeError):
    """대량 삭제 임계 초과. 실행 **전에** 전체를 중단시킨다(C5)."""


@dataclass(frozen=True)
class Action:
    kind: str
    rel_path: str
    key: str
    is_dir: bool = False
    decision: Decision | None = None
    new_rel_path: str = ""
    note: str = ""

    @property
    def size(self) -> int | None:
        return None if self.decision is None else self.decision.size


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    reports: list[tuple[str, str]] = field(default_factory=list)
    protected: list[tuple[str, str]] = field(default_factory=list)
    unsyncable: list[tuple[str, str]] = field(default_factory=list)
    deferred: list[tuple[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    delete_count: int = 0        # 실제로 사라질 항목 수(폴더는 하위 포함)
    delete_actions: int = 0      # 삭제 동작 수(폴더 1건 = Action 1건)
    bytes_up: int = 0
    bytes_down: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.actions


def _depth(key: str) -> int:
    return key.count("/")


def _under(key: str, prefix_key: str) -> bool:
    """key가 prefix_key의 **하위**인가(자기 자신은 제외)."""
    return bool(prefix_key) and key.startswith(prefix_key + "/")


def _effective_delete_count(deletes: Sequence[Decision], base_keys: Sequence[str]) -> int:
    """**실제로 사라질 항목 수.**

    폴더 삭제는 하위에 재귀 적용되므로 Action 1건이 수천 개를 지울 수 있다. 게이트가
    Action 수를 세면 가장 위험한 경우(트리 통째 삭제)에서 정확히 무력화된다 — 5,000개를
    지우는 계획이 '삭제 1건'으로 통과한다. 그래서 base 키로 서브트리 크기를 환산한다.
    """
    if not deletes:
        return 0
    total = 0
    for d in deletes:
        if not d.is_dir:
            total += 1
            continue
        prefix = d.key + "/"
        n = sum(1 for k in base_keys if k == d.key or k.startswith(prefix))
        total += max(1, n)
    return total


def plan(decisions: Sequence[Decision], *, base_count: int = 0, p: Profile | None = None,
         allow_bulk_delete: bool = False, trash_ok: bool = True,
         trash_reason: str = "", base_keys: Sequence[str] = ()) -> Plan:
    """Decision을 실행 순서의 Action으로. 안전 게이트를 통과하지 못하면 예외를 올린다."""
    prof = p or Profile()
    out = Plan()

    # 1) 보고 계열 분리
    live: list[Decision] = []
    for d in decisions:
        if d.kind == KIND_REPORT:
            out.reports.append((d.rel_path, d.reason))
        elif d.kind == KIND_PROTECT:
            out.protected.append((d.rel_path, d.reason))
        elif d.kind == KIND_UNSYNCABLE:
            out.unsyncable.append((d.rel_path, d.reason))
        else:
            live.append(d)

    # 2) send2trash를 쓸 수 없으면 로컬 삭제는 실행하지 않고 보고로 강등한다.
    #    삭제를 os.remove로 대체하지 않는 것이 이 프로젝트의 불변식이다(규약_M2 I5).
    if not trash_ok:
        keep: list[Decision] = []
        for d in live:
            if d.kind == KIND_LOCAL_TRASH:
                out.reports.append((
                    d.rel_path,
                    f"원격에서 삭제됨 — 휴지통을 쓸 수 없어 로컬은 그대로 둡니다({trash_reason or 'send2trash 없음'})"))
            else:
                keep.append(d)
        live = keep

    # 3) 폴더 삭제는 하위에 재귀 적용된다 — 자손을 또 지우려 하면 실패한다(C5).
    live = _collapse_trash_subtrees(live, out)

    # 4) 이동이 계획된 폴더의 하위는 이번 패스에서 다루지 않는다.
    #    이동 후 경로가 바뀌므로 같은 패스에서 자손을 처리하면 엉뚱한 자리에 쓴다.
    live = _defer_moved_subtrees(live, out)

    # 5) 대량 삭제 게이트 — 실행 전에 전부 막는다
    #    세는 값은 Action 수가 아니라 **실제로 사라질 항목 수**다(폴더는 하위 재귀).
    deletes = [d for d in live if d.kind in TRASH_KINDS]
    effective = _effective_delete_count(deletes, tuple(base_keys))
    out.delete_count = effective
    out.delete_actions = len(deletes)
    if deletes and not allow_bulk_delete:
        limit_n = max(0, int(getattr(prof, "bulk_delete_abort_count", 50) or 0))
        ratio = float(getattr(prof, "bulk_delete_abort_ratio", 0.20) or 0.0)
        # 비율 임계는 기준선이 충분히 클 때만 의미가 있다. 파일 5개짜리 폴더에서는
        # 20%가 1건이라 **정상적인 파일 하나 삭제조차 매번 막힌다** — 그러면 사용자가
        # 습관적으로 --allow-bulk-delete 를 붙이게 되고 안전장치가 통째로 무력화된다.
        # 작은 기준선에서는 절대 건수(기본 50) 임계만 적용한다.
        limit_r = (int(base_count * ratio)
                   if (base_count >= RATIO_MIN_BASE and ratio > 0) else 0)
        why = ""
        if limit_n and effective >= limit_n:
            why = f"삭제 대상 {effective}건 ≥ 임계 {limit_n}건"
        elif limit_r and effective >= limit_r:
            why = f"삭제 대상 {effective}건 ≥ 기준선 {base_count}건의 {ratio:.0%}({limit_r}건)"
        if why:
            sample = "\n".join(
                f"    - {d.rel_path}" + ("  (폴더 — 하위 전체)" if d.is_dir else "")
                for d in deletes[:10])
            raise BulkDeleteAbort(
                f"대량 삭제로 판단해 **아무것도 실행하지 않고** 중단했습니다: {why}\n"
                f"{sample}\n"
                f"    ... 삭제 동작 {len(deletes)}건 / 실제로 사라질 항목 {effective}건\n"
                "  의도한 삭제가 맞다면 --allow-bulk-delete 를 붙여 다시 실행하세요.\n"
                "  아니라면 원격/로컬 어느 쪽이 통째로 사라졌는지 먼저 확인하세요"
                "('dsync sync --full --dry-run')."
            )

    # 6) 정렬 + 집계
    for d in sorted(live, key=lambda x: (_ORDER.get(x.kind, 9), _depth(x.key), x.key)):
        act = Action(kind=d.kind, rel_path=d.rel_path, key=d.key, is_dir=d.is_dir,
                     decision=d, new_rel_path=d.new_rel_path, note=d.reason)
        out.actions.append(act)
        out.counts[d.kind] = out.counts.get(d.kind, 0) + 1
        size = d.size or 0
        if d.kind in (KIND_UPLOAD_NEW, KIND_UPLOAD_VERSION):
            out.bytes_up += size
        elif d.kind in (KIND_DOWNLOAD_NEW, KIND_DOWNLOAD_UPDATE):
            out.bytes_down += size
        elif d.kind == KIND_CONFLICT:
            out.bytes_down += size
    return out


def _collapse_trash_subtrees(decisions: list[Decision], out: Plan) -> list[Decision]:
    """이미 삭제 대상인 폴더의 자손 삭제를 제거한다.

    실측: 폴더를 휴지통으로 보내면 하위에 재귀 적용된다. 자식마다 다시 move를 부르면
    '이미 휴지통' 오류(HTTP 200 + resultCode=-15700100)가 무더기로 난다.

    다만 폴더 아래에 **삭제가 아닌 작업**이 하나라도 남아 있으면 그 폴더는 통째로
    지우지 않는다. 대표적인 것이 결정표 10의 '보존 승리'(로컬에서 수정된 파일을 다시
    올림)다 — 형제 폴더가 삭제된다는 이유로 그 파일까지 휴지통에 들어가면 보존 승리가
    무효가 된다. 그런 폴더의 삭제는 보고로 강등하고 다음 실행으로 미룬다.
    """
    dirs = [d for d in decisions if d.kind in TRASH_KINDS and d.is_dir]
    if not dirs:
        return decisions

    blocked: set[str] = set()
    for d in dirs:
        for other in decisions:
            if other.kind in TRASH_KINDS or other.kind in PASSIVE_KINDS:
                continue
            if _under(other.key, d.key):
                blocked.add(d.key)
                break

    keep: list[Decision] = []
    dir_keys = {(d.kind, d.key) for d in dirs if d.key not in blocked}
    for d in decisions:
        if d.kind in TRASH_KINDS and d.is_dir and d.key in blocked:
            out.reports.append((
                d.rel_path,
                "이 폴더 아래에 아직 처리할 작업이 남아 있어 폴더 삭제를 미룹니다"
                " — 다음 실행에서 다시 판단합니다"))
            out.deferred.append((d.rel_path, "하위 작업 완료 후 다음 실행에서 삭제 판단"))
            continue
        if d.kind in TRASH_KINDS and any(
                kind == d.kind and _under(d.key, dkey) for kind, dkey in dir_keys):
            continue        # 상위 폴더 삭제에 포함된다 — 조용히 빼도 결과가 같다
        keep.append(d)
    return keep


def _defer_moved_subtrees(decisions: list[Decision], out: Plan) -> list[Decision]:
    """폴더 이동이 있으면 그 하위 작업은 다음 실행으로 미룬다."""
    moved_dirs = [d.key for d in decisions if d.kind in MOVE_KINDS and d.is_dir]
    if not moved_dirs:
        return decisions
    keep: list[Decision] = []
    for d in decisions:
        if any(_under(d.key, mk) for mk in moved_dirs):
            # 이동 계열도 함께 미룬다. 폴더가 통째로 옮겨지면 자손은 이미 새 자리에 있어
            # 개별 이동은 원본을 찾지 못하고 전부 실패한다(허위 경고 + 유령 레코드).
            out.deferred.append((d.rel_path, "상위 폴더 이동에 포함됨 — 다음 실행에서 재판단"))
            continue
        keep.append(d)
    return keep
