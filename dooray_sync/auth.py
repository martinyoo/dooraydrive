"""API 토큰 보관·조회와 로그 마스킹 — 규약 §5.

- 우선순위: 환경변수 `DOORAY_API_TOKEN`(테스트용) → keyring.
  운용 시에는 keyring에 넣는다. 환경변수는 프로세스 목록·크래시 덤프에 남을 수 있다.
- 토큰은 로그·설정파일·예외 메시지에 평문으로 남기지 않는다(규약 §12-5).
  keyring 백엔드가 던진 예외 메시지도 그대로 재노출하지 않고 스크럽해서 전달한다.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Sequence

__all__ = [
    "KEYRING_SERVICE",
    "KEYRING_ACCOUNT",
    "ENV_VAR",
    "TokenNotFound",
    "get_token",
    "set_token",
    "delete_token",
    "mask",
    "TokenMaskingFilter",
]

KEYRING_SERVICE = "dooray-sync"
KEYRING_ACCOUNT = "api-token"
ENV_VAR = "DOORAY_API_TOKEN"

PLACEHOLDER = "***"

# 이보다 짧은 문자열은 스크럽 대상에서 제외한다 — 흔한 단어를 통째로 '***'로
# 바꿔 로그를 못 읽게 만드는 부작용이 토큰 노출 위험보다 크다.
_MIN_SECRET_LEN = 8

_KEYRING_HINT = "  pip install keyring"

_ISSUE_HINT = (
    "  [발급] Dooray 웹 → 개인설정 > API > 개인 인증 토큰 에서 발급\n"
    "\n"
    "  [등록] 아래 둘 중 하나로 등록하세요.\n"
    "    1) keyring (권장 — OS 자격 증명 관리자에 보관, 재부팅 후에도 유지)\n"
    "         python -c \"import keyring; keyring.set_password('dooray-sync','api-token','발급받은토큰')\"\n"
    "    2) 환경변수 (테스트용 — 현재 PowerShell 세션에서만 유효)\n"
    "         $env:DOORAY_API_TOKEN = '발급받은토큰'"
)

# keyring 조회가 실패한 마지막 사유(예외 클래스명 수준). 안내 메시지 보강용.
_last_keyring_error: str | None = None


class TokenNotFound(RuntimeError):
    """토큰을 환경변수에서도 keyring에서도 찾지 못했을 때."""


# --------------------------------------------------------------------------
# 스크럽 (내부 공용)
# --------------------------------------------------------------------------

def _scrub_text(text: str, secrets: Sequence[str]) -> str:
    for s in secrets:
        if s and s in text:
            text = text.replace(s, PLACEHOLDER)
    return text


def _known_secrets() -> list[str]:
    """지금 알고 있는 토큰 후보. 조회 실패는 조용히 무시(스크럽은 best-effort)."""
    out: list[str] = []
    env = os.environ.get(ENV_VAR, "").strip()
    if len(env) >= _MIN_SECRET_LEN:
        out.append(env)
    try:
        kr = _import_keyring()
        stored = (kr.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) or "").strip()
    except Exception:
        stored = ""
    if len(stored) >= _MIN_SECRET_LEN and stored not in out:
        out.append(stored)
    return out


def _safe_reason(exc: BaseException) -> str:
    """예외를 메시지로 옮길 때 토큰이 섞여 나가지 않도록 스크럽한다."""
    return _scrub_text(f"{type(exc).__name__}: {exc}", _known_secrets())[:300]


# --------------------------------------------------------------------------
# keyring 접근
# --------------------------------------------------------------------------

def _import_keyring() -> Any:
    try:
        import keyring  # 선택 의존성
    except ImportError as exc:
        raise RuntimeError(
            "keyring 패키지가 설치되어 있지 않습니다.\n" + _KEYRING_HINT
        ) from exc
    except Exception as exc:
        # 백엔드가 import 시점에 터지는 경우(설정 깨짐 등)까지 RuntimeError로 통일한다.
        # 여기서는 _safe_reason을 쓰지 않는다 — _known_secrets가 다시 이 함수를 불러 재귀한다.
        # (아직 토큰을 읽기 전이라 import 단계 예외에 토큰이 섞일 수 없다.)
        raise RuntimeError(f"keyring 로드 실패: {type(exc).__name__}: {exc}"[:300]) from exc
    return keyring


def _keyring_get() -> str:
    """keyring에서 토큰을 읽는다. 실패는 '없음'과 동일하게 취급하고 사유만 기록."""
    global _last_keyring_error
    try:
        kr = _import_keyring()
    except Exception as exc:
        # 토큰 조회 실패로 프로그램이 죽으면 안 된다 — 안내 메시지로 흡수한다.
        _last_keyring_error = str(exc).splitlines()[0]
        return ""
    try:
        token = kr.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) or ""
    except Exception as exc:
        # 백엔드 부재(NoKeyringError)·잠김 등. 상위에서 안내 메시지에 붙인다.
        _last_keyring_error = _safe_reason(exc)
        return ""
    _last_keyring_error = None
    return token.strip()


def _not_found_message() -> str:
    msg = (
        "API 토큰을 찾을 수 없습니다.\n"
        f"  (확인한 곳: 환경변수 {ENV_VAR}, keyring "
        f"service='{KEYRING_SERVICE}' account='{KEYRING_ACCOUNT}')\n"
        "\n" + _ISSUE_HINT
    )
    if _last_keyring_error:
        msg += f"\n\n  ※ keyring 접근 실패: {_last_keyring_error}"
    return msg


# --------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------

def get_token() -> str:
    """환경변수 우선(테스트용) → keyring. 없으면 TokenNotFound(안내 메시지 포함)."""
    token = os.environ.get(ENV_VAR, "").strip()
    if token:
        return token
    token = _keyring_get()
    if token:
        return token
    raise TokenNotFound(_not_found_message())


def set_token(token: str) -> None:
    """keyring에 저장. 환경변수는 건드리지 않는다(프로세스 밖으로 못 나감)."""
    t = (token or "").strip()
    if not t:
        raise ValueError("빈 토큰은 저장할 수 없습니다.")
    if any(ch.isspace() for ch in t):
        # 웹에서 복사할 때 개행·탭이 섞여 들어오는 사고가 잦다. 값은 메시지에 넣지 않는다.
        raise ValueError("토큰에 공백/개행이 포함되어 있습니다. 붙여넣기를 확인하세요.")
    kr = _import_keyring()
    try:
        kr.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, t)
    except Exception as exc:
        raise RuntimeError(f"keyring 저장 실패: {_safe_reason(exc)}") from None


def delete_token() -> None:
    """keyring에 보관된 토큰 삭제. 없으면 조용히 통과(멱등).

    환경변수 토큰은 이 함수로 지울 수 없다 — 셸에서 직접 해제해야 한다.
    """
    kr = _import_keyring()
    try:
        if kr.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT) is None:
            return
        kr.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except Exception as exc:
        raise RuntimeError(f"keyring 삭제 실패: {_safe_reason(exc)}") from None


def mask(token: str) -> str:
    """'ofojxrv2...(35자)' 형태. 로그 출력용."""
    if not token:
        return "(토큰 없음)"
    t = str(token)
    n = len(t)
    # 짧은 토큰이 접두 8자만으로 사실상 전부 드러나는 것을 막는다.
    head = 8 if n > 12 else max(1, n // 4)
    return f"{t[:head]}...({n}자)"


class TokenMaskingFilter(logging.Filter):
    """레코드 메시지에서 현재 토큰 문자열을 '***'로 치환.

    ※ Logger가 아니라 **Handler에 붙일 것**. Logger의 filter는 그 로거로 직접 남긴
       레코드에만 걸리고, 자식 로거에서 전파된 레코드는 그대로 통과한다.
    """

    def __init__(self, token: str | None = None, name: str = "") -> None:
        super().__init__(name)
        self._secrets: list[str] = []
        if token is None:
            self.refresh()
        else:
            self.add_secret(token)

    def add_secret(self, secret: str | None) -> None:
        s = (secret or "").strip()
        if len(s) < _MIN_SECRET_LEN or s in self._secrets:
            return
        self._secrets.append(s)

    def refresh(self) -> None:
        """토큰을 새로 등록·교체한 뒤 호출. 조회 실패는 무시(로깅이 죽으면 안 된다)."""
        for s in _known_secrets():
            self.add_secret(s)

    def _scrub(self, value: Any) -> Any:
        return _scrub_text(value, self._secrets) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        # msg와 args 양쪽 — 포매팅은 handler에서 나중에 일어나므로 둘 다 손봐야 한다.
        record.msg = self._scrub(record.msg)
        args = record.args
        if isinstance(args, dict):
            record.args = {k: self._scrub(v) for k, v in args.items()}
        elif isinstance(args, tuple):
            record.args = tuple(self._scrub(a) for a in args)
        # 트레이스백은 filter 시점에 아직 문자열이 아니다(exc_text는 Formatter가 채운다).
        # 여기서 미리 포매팅해 두면 Formatter가 그대로 재사용하므로 마스킹이 보장된다.
        # 실측: 이 처리가 없으면 `raise RuntimeError(f"... token={t}")`가 로그에 평문 노출됨.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if isinstance(record.exc_text, str):
            record.exc_text = _scrub_text(record.exc_text, self._secrets)
        if isinstance(record.stack_info, str):
            record.stack_info = _scrub_text(record.stack_info, self._secrets)
        return True


def _secrets_snapshot() -> Iterable[str]:
    """테스트 보조 — 현재 스크럽 대상 후보."""
    return tuple(_known_secrets())
