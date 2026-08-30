"""`gitlock.py` 두 사본이 갈라지는 것을 막는다.

정본은 `tools/gitlock.py`(이 저장소)이고, vault 의
`knowledge_base/scripts/gitlock.py` 는 그 사본이다.

**왜 사본이 필요한가**: Cloud Run 컨테이너가 vault 저장소만 클론해 `capture.py`
를 돌린다(`cloud_entrypoint.py`). dooraydrive 저장소는 거기 없으므로 정책이
vault 안에 있어야 한다. import 로 공유할 수 없다.

**왜 위험한가**: 이 프로젝트가 반복해 낸 사고가 정확히 이것이다 — 잠금 처리가
`lib.ps1` 과 `capture.py` 두 곳에 복제되어 각자 썩었고, 둘 다 `index.lock`
이라는 이름만 찾다가 `HEAD.lock` 을 놓쳤다(2026-08-30). 정책을 한 곳으로 모아
그 병을 고쳤는데, **모은 그 파일 자체가 다시 두 벌이 됐다.**

한 벌을 고치고 다른 벌을 잊으면 같은 사고가 재발한다. 사본이 불가피하다면
**갈라짐을 탐지하는 것**이 유일한 방어다. 이 테스트가 그 탐지기다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

CANON = Path(__file__).resolve().parent.parent / "tools" / "gitlock.py"
VAULT_COPY = Path(r"C:\drive\obsidian\knowledge_base\scripts\gitlock.py")

needs_vault = pytest.mark.skipif(
    not VAULT_COPY.exists(), reason="이 PC 에 vault 사본이 없다")


def _digest(path: Path) -> str:
    # 줄바꿈 차이는 무시한다 — .gitattributes 와 autocrlf 가 싸우는 저장소라
    # CRLF/LF 만으로 갈라졌다고 보고하면 오탐이 상시화된다.
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_canonical_copy_exists():
    assert CANON.exists(), f"정본이 없다: {CANON}"


@needs_vault
def test_vault_copy_matches_canonical():
    """두 사본이 같아야 한다.

    이 테스트가 빨간불이면 둘 중 하나만 고친 것이다. 정본(tools/gitlock.py)을
    고치고 vault 로 복사하는 것이 정해진 방향이다 — 반대 방향으로 복사하면
    이 저장소의 테스트가 검증하지 않은 코드가 정본이 된다.
    """
    assert _digest(CANON) == _digest(VAULT_COPY), (
        "gitlock.py 사본이 갈라졌다.\n"
        f"  정본 : {CANON}\n"
        f"  사본 : {VAULT_COPY}\n"
        "  정본을 고친 뒤 사본으로 복사하세요(반대 방향 금지).")


@needs_vault
def test_vault_copy_is_importable_standalone():
    """사본이 vault 에서 **단독으로** import 되어야 한다.

    dooraydrive 패키지에 기대는 import 가 하나라도 생기면 Cloud Run 컨테이너에서
    죽는다 — 거기엔 이 저장소가 없다. 그리고 그 죽음은 캡처 봇의 git 단계에서
    일어나므로 정확히 잠금 잔해를 만든다.
    """
    src = VAULT_COPY.read_text(encoding="utf-8")
    for forbidden in ("dooray_sync", "from tools", "import tools"):
        assert forbidden not in src, (
            f"사본이 dooraydrive 에 의존한다: {forbidden!r} — "
            "Cloud Run 컨테이너에서 죽는다")


def test_canonical_has_no_repo_specific_imports():
    """정본도 마찬가지 — 이 파일은 어느 저장소에 놓여도 돌아야 한다."""
    src = CANON.read_text(encoding="utf-8")
    for forbidden in ("dooray_sync", "from tests"):
        assert forbidden not in src, f"정본이 {forbidden!r} 에 의존한다"
