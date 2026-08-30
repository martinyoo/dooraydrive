"""git 잠금 시나리오 하버스 — 수정 금지 영역.

autoresearch의 `prepare.py`에 대응한다. 시나리오를 만들고 채점하는 쪽이며,
**개선 대상(`gitlock.py`)이 이 안을 고치면 실험이 무의미해진다.**

이 패키지는 실제 git 바이너리로 진짜 저장소를 만든다. 페이크를 쓰지 않는 이유는
재현해야 하는 것이 **git 자신의 잠금 의미론**이기 때문이다 — 페이크로 흉내 내면
흉내가 맞는지를 다시 검증해야 한다.

위험 표면이 크다. 잠금 하버스는 정의상 `.git` 내부를 만들고 지우므로, 경로 계산이
한 번만 어긋나도 `C:\\drive\\obsidian`의 진짜 볼트를 망가뜨린다. 그래서
`conftest.py`의 autouse 가드와 `tests/test_gitlock_isolation.py`가 **하버스 코드보다
먼저** 존재한다.
"""
from __future__ import annotations

__all__ = ["REAL_VAULT_ROOTS", "assert_outside_real_vaults"]

import os
from pathlib import Path

# 이 PC의 진짜 볼트. 하버스 산출물이 이 안으로 들어가면 즉시 실패시킨다.
# 하드코딩이 아니라 "알려진 위험 위치"의 목록이다 — 없으면 없는 대로 넘어간다.
REAL_VAULT_ROOTS: tuple[Path, ...] = (
    Path(r"C:\drive\obsidian"),
    Path(r"D:\drive\obsidian"),          # 데스크톱(HSY) 구성
    Path(r"D:\drive\dooraydrive\obsidian"),
)


def assert_outside_real_vaults(path: Path) -> None:
    """경로가 진짜 볼트 밖인지 확인. 안이면 AssertionError.

    하버스의 모든 저장소 생성 지점이 이것을 통과해야 한다. 검사를 호출부마다
    기억하게 두지 않고, 저장소를 만드는 단 하나의 함수에서 부른다.
    """
    resolved = Path(path).resolve()
    for root in REAL_VAULT_ROOTS:
        try:
            resolved.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        raise AssertionError(
            f"하버스가 실제 볼트 안에 저장소를 만들려 했습니다: {resolved}\n"
            f"  볼트 루트: {root}\n"
            f"  이 검사가 없으면 사용자의 진짜 .git 이 망가집니다.")
    # tmp 밖도 막는다 — 볼트 목록에 없는 실사용 폴더가 언젠가 생긴다.
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if tmp:
        try:
            resolved.relative_to(Path(tmp).resolve())
            return
        except (ValueError, OSError):
            pass
    raise AssertionError(
        f"하버스 저장소가 임시 폴더 밖입니다: {resolved}\n"
        f"  TEMP={tmp!r}. pytest tmp_path 를 쓰지 않은 경로일 수 있습니다.")
