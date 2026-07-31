"""로거 구성 — 파일(UTF-8 회전) + 콘솔, TokenMaskingFilter 부착.

- 파일 핸들러는 `state_dir/logs/dsync.log`. 진단의 정본이므로 콘솔보다 상세하다.
- 콘솔 핸들러는 **stderr**로 보낸다. stdout은 명령의 보고(표·요약)만 싣는다 —
  `dsync status > out.txt` 같은 사용에서 로그가 섞이지 않게.
- `TokenMaskingFilter`는 로거가 아니라 **핸들러**에 붙인다(auth.py 주석):
  로거의 filter는 자식 로거에서 전파된 레코드를 통과시켜 마스킹이 새어 나간다.
- 파일 IO는 `ext_path()` 경유(규약 §0-2). 로그 경로는 짧지만 예외를 두지 않는다.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .auth import TokenMaskingFilter
from .config import log_dir
from .util.paths import ext_path

__all__ = [
    "LOGGER_NAME",
    "LOG_FILE_NAME",
    "setup_logging",
    "get_logger",
    "current_log_path",
]

LOGGER_NAME = "dooray_sync"
LOG_FILE_NAME = "dsync.log"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_CONSOLE_FORMAT = "%(levelname)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 이 모듈이 붙인 핸들러 표식. 같은 프로세스에서 setup_logging이 두 번 불려도
# (테스트·재진입) 핸들러가 중복되지 않게 우리 것만 골라 제거한다.
_TAG = "_dooray_sync_owned"

_log_path: Path | None = None


def _detach_owned(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _TAG, False):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def _mark(handler: logging.Handler, masker: TokenMaskingFilter) -> logging.Handler:
    setattr(handler, _TAG, True)
    handler.addFilter(masker)
    return handler


def _file_handler(path: Path, level: int, masker: TokenMaskingFilter) -> logging.Handler | None:
    """회전 파일 핸들러. 실패해도 CLI를 죽이지 않는다 — 로그가 없어도 명령은 돌아야 한다."""
    try:
        os.makedirs(ext_path(path.parent), exist_ok=True)
        h = RotatingFileHandler(
            ext_path(path),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",     # 규약 §0-1: 한국어 로그가 cp949로 emit되면 크래시한다
            delay=True,           # 실제 로그가 날 때까지 파일을 만들지 않는다
        )
    except OSError:
        return None
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    return _mark(h, masker)


def _console_handler(level: int, masker: TokenMaskingFilter) -> logging.Handler:
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(level)
    h.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    return _mark(h, masker)


def setup_logging(
    profile: str = "default",
    *,
    verbose: bool = False,
    quiet: bool = False,
    log_directory: Path | None = None,
) -> logging.Logger:
    """`dooray_sync` 로거를 구성하고 돌려준다.

    - verbose: 콘솔 INFO + 파일 DEBUG (기본은 콘솔 WARNING + 파일 INFO)
    - quiet: 콘솔 ERROR만 (파일 기록은 그대로 — 사후 진단을 잃지 않는다)
    - log_directory: 설정 경로 대신 쓸 디렉터리(테스트용)
    """
    global _log_path

    console_level = logging.ERROR if quiet else (logging.INFO if verbose else logging.WARNING)
    file_level = logging.DEBUG if verbose else logging.INFO

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(min(console_level, file_level))
    # 루트로 올려 보내면 basicConfig가 있는 환경에서 같은 줄이 두 번 찍힌다.
    logger.propagate = False
    _detach_owned(logger)

    masker = TokenMaskingFilter()

    try:
        directory = Path(log_directory) if log_directory is not None else log_dir(profile)
    except ValueError:
        # 프로파일 이름이 잘못된 경우 — 상위에서 설정 오류로 다시 걸린다.
        directory = None  # type: ignore[assignment]

    _log_path = None
    if directory is not None:
        path = Path(directory) / LOG_FILE_NAME
        fh = _file_handler(path, file_level, masker)
        if fh is not None:
            logger.addHandler(fh)
            _log_path = path

    logger.addHandler(_console_handler(console_level, masker))
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """`dooray_sync` 하위 로거. setup_logging 이후에 쓴다."""
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")


def current_log_path() -> Path | None:
    """마지막 setup_logging이 연 로그 파일 경로(없으면 None). status/doctor 출력용."""
    return _log_path
