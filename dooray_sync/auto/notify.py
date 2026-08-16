"""통지 — 무인 실행이 사람에게 남기는 말.

**창이 떠 있다는 것과 사람이 본다는 것은 다르다.** 창은 최소화돼 있고 8시간
동안 스크롤이 흐른다. 그래서 사람 손이 닿아야 하는 일(보류·설정 오류)은
notices.jsonl에 남기고, **사람이 이미 보는 화면**에서 다시 꺼내 보여 준다 —
폴더에서 synchere.bat을 무인자로 실행할 때 맨 앞에 찍는 블록이 그것이다.
새 UI를 만들지 않는 것이 요점이다(토스트·별도 창은 만들 것이 늘고 인코딩
위험이 붙는다).

**원인이 해소되면 스스로 사라진다.** 사람이 '확인' 버튼을 누르는 방식이면
아무도 안 누르고, 그러면 목록이 노이즈가 되어 진짜 신호를 묻는다. 다음 성공
실행이 그 프로파일의 통지를 지운다.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from ..util.paths import ext_path
from .state import auto_dir

__all__ = ["add", "clear", "load", "format_block", "notices_path"]

# 사람이 읽는 이름. 종료코드가 아니라 '무엇을 해야 하는가'로 나눈다.
KIND_LABEL = {
    "held": "보류",
    "config": "설정",
    "deletes": "삭제 대기",
    "conflicts": "충돌 대기",
    "error": "오류",
}


def notices_path():
    return auto_dir() / "notices.jsonl"


def load() -> list[dict[str, Any]]:
    try:
        with open(ext_path(notices_path()), "rb") as f:
            raw = f.read().decode("utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _write(items: list[dict[str, Any]]) -> None:
    dest = notices_path()
    try:
        os.makedirs(ext_path(dest.parent), exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
        with open(ext_path(tmp), "w", encoding="utf-8", newline="\n") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(ext_path(tmp), ext_path(dest))
    except OSError:
        pass          # 통지 기록 실패가 동기화를 막지 않는다


def add(profile: str, kind: str, message: str, *, ts: str = "") -> None:
    """같은 (프로파일, 종류)는 갱신한다 — 2분마다 같은 줄이 쌓이면 목록이
    노이즈가 되고, 그 노이즈가 진짜 신호를 묻는다."""
    items = [i for i in load()
             if not (i.get("profile") == profile and i.get("kind") == kind)]
    items.append({"ts": ts, "profile": profile, "kind": kind, "message": message})
    _write(items)


def clear(profile: str, kind: str | None = None) -> None:
    """원인 해소 시 호출. kind가 없으면 그 프로파일 전부."""
    items = [
        i for i in load()
        if not (i.get("profile") == profile
                and (kind is None or i.get("kind") == kind))
    ]
    _write(items)


def format_block(items: list[dict[str, Any]] | None = None) -> str:
    """무인자 synchere 실행의 맨 앞에 찍을 블록. 없으면 빈 문자열."""
    items = load() if items is None else items
    if not items:
        return ""
    lines = [f"{'=' * 16} 자동 동기화 알림 {len(items)}건 {'=' * 16}"]
    for i in sorted(items, key=lambda x: str(x.get("ts") or "")):
        label = KIND_LABEL.get(str(i.get("kind")), str(i.get("kind")))
        ts = str(i.get("ts") or "")[:16].replace("T", " ")
        lines.append(f" [{label}] {ts}  {i.get('profile')}  {i.get('message')}")
    lines.append("=" * (34 + len(f" 자동 동기화 알림 {len(items)}건 ")))
    return "\n".join(lines)
