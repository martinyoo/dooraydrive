"""단일 인스턴스 잠금 (msvcrt 배타 잠금 + PID 기록).

두 인스턴스가 같은 상태 DB와 같은 로컬 루트를 동시에 만지는 것이 이 프로그램에서
가장 위험한 상황이므로, Windows 전용 API가 없으면 우회하지 않고 즉시 실패한다.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

from .paths import ext_path

try:
    import msvcrt
except ImportError as e:  # 비Windows — graceful degradation 금지
    raise RuntimeError(
        "dooray-sync는 Windows 전용입니다(msvcrt 없음). 잠금 없이는 실행할 수 없습니다."
    ) from e

# 잠그는 구간은 0번 바이트 1개뿐이고, PID 정보는 1번 오프셋부터 쓴다.
# Windows의 바이트 범위 잠금은 강제적(mandatory)이라 잠긴 구간은 다른 프로세스에서
# 읽기조차 실패한다 — 정보를 잠금 구간 밖에 둬야 대기 측이 보유자를 알 수 있다.
_LOCK_OFFSET = 0
_LOCK_BYTES = 1
_INFO_OFFSET = 1
_INFO_MAX = 512


class AlreadyRunning(RuntimeError):
    """다른 인스턴스가 이미 잠금을 보유 중."""


def _read_info(fh) -> str:
    try:
        fh.seek(_INFO_OFFSET)
        raw = fh.read(_INFO_MAX)
    except OSError:
        return "보유 프로세스 정보 없음"
    text = raw.decode("utf-8", "replace").strip()
    return text or "보유 프로세스 정보 없음"


class SingleInstanceLock:
    """msvcrt.locking 기반 배타 잠금 + PID 기록. with 문 사용."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._fh = None
        self._locked = False

    def acquire(self) -> None:
        if self._locked:
            return
        os.makedirs(ext_path(self.lock_path.parent), exist_ok=True)
        fd = os.open(
            ext_path(self.lock_path),
            os.O_RDWR | os.O_CREAT | os.O_BINARY,
            0o600,
        )
        fh = os.fdopen(fd, "r+b")
        try:
            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
        except OSError:
            holder = _read_info(fh)
            fh.close()
            raise AlreadyRunning(
                f"이미 실행 중입니다 ({holder}). lock={self.lock_path}"
            ) from None
        self._fh = fh
        self._locked = True
        self._write_info()

    def _write_info(self) -> None:
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        payload = f"pid={os.getpid()} since={stamp}\n".encode("utf-8")[:_INFO_MAX]
        self._fh.seek(_INFO_OFFSET)
        self._fh.write(payload)
        self._fh.truncate(_INFO_OFFSET + len(payload))  # 이전 보유자의 낡은 기록 제거
        self._fh.flush()

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if self._locked:
                fh.seek(_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
        except OSError:
            pass  # 이미 풀렸거나 핸들 무효 — close가 어차피 해제한다
        finally:
            self._locked = False
            fh.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
