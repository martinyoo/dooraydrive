r"""로컬 삭제 = 휴지통 이동 (규약_M2 §1, 구현계획서 C5).

이 모듈의 존재 이유는 하나다: **사용자 파일을 되돌릴 수 없게 지우는 경로를 만들지 않는 것.**
`send2trash`가 없거나 실패하면 `os.remove`/`shutil.rmtree`로 대체하지 않고 `TrashUnavailable`을
올린다. 호출측은 그 항목을 '실패'로 보고해야 하며, 삭제를 강행해서는 안 된다.

send2trash는 Windows에서 셸 파일 조작 API(IFileOperation/SHFileOperation)를 쓴다. 이 API는
`\\?\` 확장 접두를 받지 못하므로, 이 모듈만은 일반 절대경로를 넘긴다. 존재 확인은 반대로
반드시 `ext_path` 경유로 한다 — 260자를 넘는 경로가 실제로 존재하기 때문이다(C3).
"""
from __future__ import annotations

import os
from pathlib import Path

from .paths import ext_path, plain_abs

__all__ = ["TrashUnavailable", "trash_available", "send_to_trash", "unavailable_reason"]


class TrashUnavailable(RuntimeError):
    """휴지통 이동을 수행할 수 없다. **삭제를 다른 방법으로 대체하지 말 것.**"""


_HINT = (
    "휴지통 이동에 필요한 send2trash 패키지를 쓸 수 없습니다.\n"
    "  설치: pip install send2trash\n"
    "  설치 전에는 삭제 전파가 수행되지 않고 '보고'로만 처리됩니다."
)


def _import_send2trash():
    from send2trash import send2trash  # noqa: PLC0415 — 선택 의존성이라 지연 임포트

    return send2trash


def unavailable_reason() -> str | None:
    """쓸 수 없으면 사유 문자열, 쓸 수 있으면 None. doctor·planner 게이트용."""
    try:
        _import_send2trash()
    except Exception as exc:  # ImportError 외에 DLL 로드 실패 등도 여기로 온다
        return f"{type(exc).__name__}: {exc}"
    return None


def trash_available() -> bool:
    return unavailable_reason() is None


def send_to_trash(path: Path | str) -> None:
    r"""Windows 휴지통으로 보낸다. 대상이 없으면 조용히 반환한다(멱등).

    파일·폴더 모두 처리하며, 폴더는 하위에 재귀 적용된다(C5 — 하위 항목마다 다시 부르지 않는다).
    """
    target = str(path or "")
    if not target:
        raise ValueError("빈 경로는 휴지통으로 보낼 수 없습니다")

    if not os.path.exists(ext_path(target)):
        return  # 이미 없음 — 삭제 목적은 달성된 상태

    reason = unavailable_reason()
    if reason is not None:
        raise TrashUnavailable(f"{_HINT}\n  원인: {reason}")

    # 셸 API는 \\?\ 접두를 인식하지 못한다. 접두 없는 절대경로로 되돌려 넘긴다.
    plain = plain_abs(target)
    try:
        _import_send2trash()(plain)
    except Exception as exc:
        # 긴 경로·권한·잠금 등으로 실패할 수 있다. 여기서 os.remove로 내려가지 않는다.
        raise TrashUnavailable(
            f"휴지통으로 보내지 못했습니다: {plain}\n  원인: {type(exc).__name__}: {exc}"
        ) from exc
