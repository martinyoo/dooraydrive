"""drive_id를 이 PC의 설정에서 얻는다 — 저장소에 특정 계정의 값을 박지 않기 위해.

예전에는 도구 세 개가 각자 `DEFAULT_DRIVE = "<내 드라이브 id>"`를 들고 있었다.
두 가지가 나빴다:

- **공개 저장소에 계정 자원 식별자가 남는다.** `docs/현재상태.md`가 새 PC 절차
  문서를 vault로 옮기며 *"drive_id가 들어가 public인 이 저장소에는 둘 수 없다"*고
  이미 원칙을 세워 놓고, 정작 코드에는 다섯 곳이 남아 있었다(2026-09-14 실측).
- **동료 PC에서는 그 기본값이 남의 드라이브다.** 기본값으로서 의미가 없을 뿐
  아니라, 인자를 빠뜨린 실행이 엉뚱한 드라이브를 조회하게 만든다.

해석 순서는 명시 인자 → 환경변수 → config.toml이다. 어디에도 없으면 **추측하지
않고** 사용법을 안내한다(하드코딩 기본값으로 떨어지지 않는다).
"""
from __future__ import annotations

import os
import tomllib

from dooray_sync.config import config_path

__all__ = ["ENV_DRIVE_ID", "DriveIdNotFound", "from_config", "resolve"]

ENV_DRIVE_ID = "DOORAY_DRIVE_ID"


class DriveIdNotFound(RuntimeError):
    """drive_id를 특정할 수 없다. 메시지에 다음 수단이 적혀 있다."""


def from_config() -> list[str]:
    """config.toml의 프로파일들이 쓰는 drive_id(중복 제거, 등장 순서)."""
    try:
        with open(config_path(), "rb") as f:
            doc = tomllib.load(f)
    except OSError:
        return []
    except tomllib.TOMLDecodeError:
        return []
    out: list[str] = []
    for body in (doc.get("profile") or {}).values():
        did = str((body or {}).get("drive_id") or "").strip()
        if did and did not in out:
            out.append(did)
    return out


def resolve(explicit: str | None = None) -> str:
    """쓸 drive_id 하나. 못 정하면 DriveIdNotFound."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get(ENV_DRIVE_ID, "").strip()
    if env:
        return env
    found = from_config()
    if len(found) == 1:
        return found[0]
    if not found:
        raise DriveIdNotFound(
            "drive_id를 찾을 수 없습니다.\n"
            "  이 PC에 프로파일이 하나라도 등록돼 있으면 자동으로 찾습니다 —\n"
            "  동기화할 폴더에 synchere.bat을 복사해 실행하면 등록됩니다.\n"
            "  직접 지정하려면: 인자로 넘기거나  set "
            f"{ENV_DRIVE_ID}=<drive_id>")
    raise DriveIdNotFound(
        "config에 drive_id가 여러 개라 하나를 고를 수 없습니다: "
        + ", ".join(found)
        + f"\n  인자로 넘기거나  set {ENV_DRIVE_ID}=<drive_id>  로 지정하세요.")
