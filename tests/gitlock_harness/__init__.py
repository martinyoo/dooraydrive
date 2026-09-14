"""git 잠금 시나리오 하버스 — 수정 금지 영역.

autoresearch의 `prepare.py`에 대응한다. 시나리오를 만들고 채점하는 쪽이며,
**개선 대상(`gitlock.py`)이 이 안을 고치면 실험이 무의미해진다.**

이 패키지는 실제 git 바이너리로 진짜 저장소를 만든다. 페이크를 쓰지 않는 이유는
재현해야 하는 것이 **git 자신의 잠금 의미론**이기 때문이다 — 페이크로 흉내 내면
흉내가 맞는지를 다시 검증해야 한다.

위험 표면이 크다. 잠금 하버스는 정의상 `.git` 내부를 만들고 지우므로, 경로 계산이
한 번만 어긋나도 이 PC의 진짜 볼트를 망가뜨린다. 그래서
`conftest.py`의 autouse 가드와 `tests/test_gitlock_isolation.py`가 **하버스 코드보다
먼저** 존재한다.
"""
from __future__ import annotations

__all__ = ["REAL_VAULT_ROOTS", "assert_outside_real_vaults"]

import os
import tempfile
from pathlib import Path

from tests import machine

# 이 PC의 진짜 볼트. 하버스 산출물이 이 안으로 들어가면 즉시 실패시킨다.
#
# 예전에는 실경로 세 개를 그대로 박아 두었다(랩탑·데스크톱 구성). 공개 저장소에
# 남길 것이 아니고, 무엇보다 **동료 PC의 볼트는 그 목록에 없어 보호되지 않는다** —
# 하드코딩한 목록은 자기 PC만 지킨다. 환경변수 하나로 이 PC의 볼트를 지목한다.
#
# 비어 있어도 안전은 유지된다: 아래 `assert_outside_real_vaults`의 **임시 폴더
# 검사가 진짜 방어선**이고(임시 폴더 밖은 전부 거부), 이 목록은 그중 가장 치명적인
# 경우에 더 나은 오류 메시지를 주는 보조선이다.
REAL_VAULT_ROOTS: tuple[Path, ...] = tuple(
    p for p in (machine.vault_root(),) if p is not None
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
    #
    # 후보를 여럿 보는 이유: **pytest의 tmp_path는 %TEMP%가 아니라
    # tempfile.gettempdir() 아래에 생기고, 그 둘은 갈릴 수 있다.**
    # gettempdir()은 TMPDIR → TEMP → TMP 순으로 보므로, 압축 유틸리티 같은
    # 프로그램이 TMPDIR을 자기 폴더로 돌려놓으면 %TEMP%와 어긋난다(2026-09-14
    # 실측: gettempdir()이 ...\Documents\ESTsoft\CreatorTemp). TEMP만 보던
    # 이전 판은 pytest가 만들어 준 tmp_path를 '임시 폴더 밖'으로 판정해
    # 하버스 전체를 setup 에러로 죽였다(42건 + test_tmp_path_is_accepted 실패).
    #
    # 가드는 여전히 fail-closed다 — 아래 후보 어디에도 속하지 않으면 거부한다.
    # 위의 REAL_VAULT_ROOTS 검사는 이보다 먼저 돌므로, 임시 폴더가 볼트 안으로
    # 지정되는 병리적 경우에도 볼트 보호가 우선한다.
    candidates = tuple(
        c for c in (tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP")) if c
    )
    for tmp in candidates:
        try:
            resolved.relative_to(Path(tmp).resolve())
            return
        except (ValueError, OSError):
            continue
    raise AssertionError(
        f"하버스 저장소가 임시 폴더 밖입니다: {resolved}\n"
        f"  임시 폴더 후보={candidates!r}. pytest tmp_path 를 쓰지 않은 경로일 수 있습니다.")
