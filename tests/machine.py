"""이 PC에 대한 사실 — **저장소에 실경로를 박지 않기 위한 단일 창구.**

이 저장소는 공개돼 있고 동료도 clone한다. 특정 PC의 폴더 구조·계정 이름이
코드에 남으면 두 가지가 동시에 나빠진다: 남의 PC에서는 그 경로가 없어 검사가
조용히 꺼지고, 내 쪽은 업무 폴더 구성이 공개된다.

**실측이 그 둘을 다 보여줬다(2026-09-14).** vault를 참조하는 테스트 네 벌이
각자 `C:\\drive\\obsidian\\...`을 직접 박아 두었는데, 그 경로는 랩탑 구성이라
**데스크톱에도 없었다.** 그래서 네 파일이 전부 skip됐고 — skip은 조용한 통과라
초록불에 묻혀 — `gitlock.py` 사본 갈라짐 탐지기가 꺼져 있는 줄 아무도 몰랐다.

그래서 규칙은 둘이다:

1. 경로는 **환경변수로 주입**한다. 저장소에는 어떤 실경로도 두지 않는다.
2. 미설정이면 건너뛰되 **사유에 켜는 방법을 적는다.** 이유가 화면에 안 남으면
   영원히 안 켜진다(이번에 실제로 그랬다).

    setx DSYNC_VAULT_ROOT "<Obsidian vault 루트>"     (새 셸부터 적용)
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["ENV_VAULT_ROOT", "REAL_USER_HOME", "vault_root", "resolve"]

ENV_VAULT_ROOT = "DSYNC_VAULT_ROOT"

# 진짜 사용자 홈. **import 시점에 한 번** 붙잡는다 — conftest의 `_isolate_git_env`가
# 테스트마다 HOME·USERPROFILE을 임시 폴더로 덮으므로, 테스트 안에서 다시 물으면
# 가짜(격리된 홈)가 돌아온다. "격리가 진짜 홈을 피했는가"를 검사하는 쪽은 덮이기
# 전의 값을 알아야 한다. conftest가 이 모듈을 import해 캡처 시점을 가장 앞으로 당긴다.
REAL_USER_HOME = Path(os.path.expanduser("~")).resolve()

_HOWTO = (
    f"{ENV_VAULT_ROOT} 미설정 — vault를 참조하는 검사를 건너뜁니다. "
    f'켜려면: setx {ENV_VAULT_ROOT} "<Obsidian vault 루트>"'
)


def vault_root() -> Path | None:
    """이 PC의 Obsidian vault 루트. 미설정·부재면 None."""
    raw = os.environ.get(ENV_VAULT_ROOT, "").strip().strip('"')
    if not raw:
        return None
    try:
        p = Path(raw)
    except (TypeError, ValueError):
        return None
    return p if p.is_dir() else None


def resolve(*parts: str) -> tuple[Path | None, str]:
    """vault 안의 파일을 가리킨다. (경로|None, skip 사유) 를 돌려준다.

    skipif의 두 인자를 한 번에 만들기 위한 모양이다:

        LIB, _WHY = machine.resolve("agent_base", "deploy", "lib.ps1")
        needs_lib = pytest.mark.skipif(LIB is None, reason=_WHY)
    """
    root = vault_root()
    rel = "/".join(parts)
    if root is None:
        return None, _HOWTO
    target = root.joinpath(*parts)
    if not target.exists():
        return None, f"{ENV_VAULT_ROOT}({root}) 아래에 없습니다: {rel}"
    return target, ""
