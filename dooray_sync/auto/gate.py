"""편입 게이트 — `--auto on`이 프로파일을 자동 대상으로 받아들이기 전의 사전 검사.

**fail-closed다.** 하나라도 걸리면 켜지 않는다. 무인 실행은 사람이 화면을 보고
"어? 이건 아닌데" 하고 Ctrl+C를 누를 기회가 없으므로, 그 판단을 켜는 시점으로
앞당긴다. 검사 항목은 전부 "이 상태로 무인 실행하면 사람이 안 보는 사이
되돌리기 어려운 일이 일어난다"에 해당하는 것들이다.

여기서 하는 일은 **판정과 사유 문장 생산**뿐이다. config를 쓰지 않는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..config import Profile, db_path
from ..store.db import Store
from ..util.paths import ext_path

__all__ = ["GateResult", "check_profile"]

# 기준선 없이 무인 실행을 시작하면 첫 사이클이 대량 충돌을 만든다. WORK 실측:
# 기준선 0건 상태에서 충돌보존 10,895건(그중 10,879건이 대조 예산 초과분).
MIN_BASELINE = 1


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)   # 거부 사유(사람이 읽는 문장)
    notes: list[str] = field(default_factory=list)     # 통과했지만 알아둘 것

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)


def check_profile(p: Profile) -> GateResult:
    """프로파일 하나가 자동 대상이 될 수 있는지. 네트워크를 쓰지 않는다 —
    `--auto status`가 '네트워크 0건'을 보장해야 하고, 같은 함수를 쓴다."""
    r = GateResult(ok=True)

    # 1) 정책 — sync_mode가 sync가 아니면 자동은 애초에 성립하지 않는다.
    #    push/pull/태그 없는 off는 사람의 결정이라 자동이 뒤집으면 안 된다(I-A3).
    if p.sync_mode != "sync":
        r.fail(f"sync_mode={p.sync_mode!r} — 자동 동기화는 'sync'인 프로파일만 받습니다"
               + (f" (사유: {p.sync_note})" if p.sync_note else ""))

    # 2) 로컬 루트가 실제로 있어야 한다. 없는 루트를 자동 대상으로 두면
    #    로컬 붕괴 판정이 매 틱 걸려 보류만 쌓인다.
    if not p.local_root:
        r.fail("local_root가 비어 있습니다")
    elif not os.path.isdir(ext_path(p.root_path)):
        r.fail(f"로컬 루트가 없습니다: {p.local_root}")

    # 3) 원격 결합이 확정돼 있어야 한다 — 자동 경로는 원격 경로를 유도하지
    #    않는다(I-A6: 무인은 미등록 폴더를 등록하지 않는다).
    if not p.drive_id:
        r.fail("drive_id가 비어 있습니다 — 'dsync init'으로 등록을 마치세요")

    db = db_path(p.name)
    if not os.path.exists(ext_path(db)):
        r.fail(f"상태 DB가 없습니다 — 먼저 한 번 수동으로 동기화하세요: {db}")
        return r

    with Store(db) as store:
        total = store.count_files(p.drive_id)
        incomplete = list(store.iter_incomplete())
        conflicts = list(store.iter_unresolved())

    # 4) 기준선 — 비어 있으면 첫 무인 사이클이 대량 충돌을 만든다(WORK 실측).
    if total < MIN_BASELINE:
        r.fail("기준선이 비어 있습니다 — 무인 실행의 첫 사이클이 대량 충돌을 만듭니다. "
               "'dsync reconcile'로 기준선을 먼저 세우세요")

    # 5) 미완료 저널 — 직전 실행이 도중에 죽었다는 뜻이다. 그 위에서 무인 주기를
    #    시작하지 않는다. 사람이 한 번 보고 넘어가야 한다(다음 수동 실행의
    #    recover()가 정리한다).
    if incomplete:
        r.fail(f"미완료 저널 {len(incomplete)}건 — 직전 실행이 중단됐습니다. "
               "폴더에서 synchere.bat을 한 번 실행해 정리한 뒤 다시 켜세요")

    # 6) 미해결 충돌 — 자동은 충돌을 처리하지 않으므로(I-A2) 쌓인 채로 시작하면
    #    영원히 쌓인다. 거부는 아니고 경고다(사람이 알고 켜면 된다).
    if conflicts:
        r.notes.append(f"미해결 충돌 {len(conflicts)}건 — 자동 실행은 충돌을 "
                       "처리하지 않습니다('dsync resolve'로 정리하세요)")

    # 7) 삭제 전파 — 켜져 있으면 무인 삭제 상한이 실제로 하중을 받는다.
    if p.propagate_deletes:
        r.notes.append("propagate_deletes=true — 무인 실행은 소량만 자동 삭제하고 "
                       "임계를 넘으면 보류합니다")

    return r
