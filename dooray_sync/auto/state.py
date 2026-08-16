"""자동 루프의 런타임 상태 — %LOCALAPPDATA%\\dooray-sync\\auto\\.

프로그램 폴더 밖이라 폴더 통째 교체(설치.bat 갱신)에 영향받지 않는다.
config(정책)와 분리한다 — 이쪽은 '지금 어디까지 했나'라서 사람이 편집할 일이 없다.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from ..config import _state_root  # noqa: PLC2701 — 상태 루트의 단일 정본
from ..util.paths import ext_path

__all__ = ["auto_dir", "state_path", "load_state", "save_state", "AutoState"]


def auto_dir() -> Path:
    """루프의 상태·로그 폴더. 만들지 않는다(호출측이 정한다)."""
    return _state_root() / "auto"


def state_path() -> Path:
    return auto_dir() / "state.json"


def load_state() -> dict[str, Any]:
    """없거나 깨졌으면 빈 상태. 상태 파일 하나 때문에 루프가 안 도는 일은 없다 —
    잃는 것은 '오늘 출근 시각' 정도이고 다음 틱이 다시 세운다."""
    try:
        with open(ext_path(state_path()), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(data: dict[str, Any]) -> None:
    """임시파일 → os.replace 원자 교체. 실패는 삼킨다(관측 수단이지 실행이 아니다)."""
    dest = state_path()
    try:
        os.makedirs(ext_path(dest.parent), exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
        with open(ext_path(tmp), "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(ext_path(tmp), ext_path(dest))
    except OSError:
        pass


class AutoState:
    """state.json의 얇은 래퍼. 키를 문자열로 흩뿌리지 않기 위한 것뿐이다."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(data) if data else load_state()

    # ---------- 하루 경계 ----------
    @property
    def last_tick(self) -> str:
        return str(self.data.get("last_tick") or "")

    @last_tick.setter
    def last_tick(self, value: str) -> None:
        self.data["last_tick"] = value

    @property
    def day_start(self) -> str:
        """오늘 '출근'으로 판정된 시각(ISO). 퇴근 스윕 시각의 기준."""
        return str(self.data.get("day_start") or "")

    @day_start.setter
    def day_start(self, value: str) -> None:
        self.data["day_start"] = value

    @property
    def last_eod_date(self) -> str:
        """퇴근 스윕을 마친 날짜(YYYY-MM-DD). 하루 1회 보장."""
        return str(self.data.get("last_eod_date") or "")

    @last_eod_date.setter
    def last_eod_date(self, value: str) -> None:
        self.data["last_eod_date"] = value

    # ---------- 프로파일별 ----------
    def profile(self, name: str) -> dict[str, Any]:
        profiles = self.data.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            self.data["profiles"] = profiles
        entry = profiles.setdefault(name, {})
        return entry if isinstance(entry, dict) else profiles.setdefault(name, {})

    def backoff_mult(self, name: str) -> float:
        try:
            return max(1.0, min(8.0, float(self.profile(name).get("backoff_mult", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    def set_backoff_mult(self, name: str, value: float) -> None:
        self.profile(name)["backoff_mult"] = max(1.0, min(8.0, float(value)))

    def save(self) -> None:
        save_state(self.data)
