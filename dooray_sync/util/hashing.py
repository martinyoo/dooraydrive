"""파일 해시.

실측: Dooray changes API의 hash 필드는 MD5 소문자 hex (PoC-05, 2회 교차검증).
따라서 원격/로컬 내용 동일성 비교는 전부 이 모듈의 값으로 판정한다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .paths import ext_path

CHUNK = 1024 * 1024


def _new_md5():
    # 보안 용도가 아니라 원격 hash 필드와의 대조용이다 — FIPS 모드에서 막히지 않도록 명시
    try:
        return hashlib.md5(usedforsecurity=False)
    except TypeError:
        return hashlib.md5()


def md5_file(path: Path | str) -> str:
    """스트리밍 MD5, 소문자 hex. ext_path 경유(규약 §12-4)."""
    h = _new_md5()
    with open(ext_path(path), "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def md5_bytes(data: bytes) -> str:
    h = _new_md5()
    h.update(data)
    return h.hexdigest()
