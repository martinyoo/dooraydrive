"""오류 분류 — "무엇이 고장났고, 사람이 무엇을 해야 하는가".

무인 실행에는 화면을 보고 판단하는 사람이 없다. 종료코드 하나로는
"와이파이가 끊겼다"와 "Dooray가 죽었다"와 "토큰이 만료됐다"를 구분할 수 없는데,
셋은 사람이 할 일이 완전히 다르다 — 각각 기다린다 / 기관에 문의한다 /
토큰을 다시 넣는다.

분류 기준은 **다음 행동**이다. 기술적 정확도가 아니라.
"""
from __future__ import annotations

__all__ = ["Fault", "classify", "LABEL", "ADVICE", "is_transient"]


class Fault:
    """분류 값. 문자열 상수로 두어 JSON에 그대로 실린다."""

    OK = "ok"
    PARTIAL = "partial"                 # 일부 파일 실패 — 고장이 아니다
    NETWORK_OFFLINE = "network_offline"  # 이름 해석 실패·연결 불가 = 이쪽 네트워크
    NETWORK_UNSTABLE = "network_unstable"  # 전송 중 끊김·타임아웃
    SERVICE_DOWN = "service_down"       # 5xx — 서버가 아프다
    SERVICE_ERROR = "service_error"     # 200 + 음수 resultCode — API 논리 거부
    RATE_LIMITED = "rate_limited"       # 429
    AUTH = "auth"                       # 401/403, 토큰 없음·만료·권한
    LOCAL = "local"                     # 로컬 파일시스템(권한·잠김·디스크)
    CONFIG = "config"                   # 설정 문제
    HELD = "held"                       # 무인 보류(사람 확인 필요)
    UNKNOWN = "unknown"


LABEL = {
    Fault.OK: "완료",
    Fault.PARTIAL: "일부 실패",
    Fault.NETWORK_OFFLINE: "네트워크 끊김",
    Fault.NETWORK_UNSTABLE: "네트워크 불안정",
    Fault.SERVICE_DOWN: "Dooray 서버 오류",
    Fault.SERVICE_ERROR: "Dooray가 요청을 거부",
    Fault.RATE_LIMITED: "요청 한도 초과",
    Fault.AUTH: "인증·권한",
    Fault.LOCAL: "로컬 파일 오류",
    Fault.CONFIG: "설정 문제",
    Fault.HELD: "보류",
    Fault.UNKNOWN: "알 수 없는 오류",
}

ADVICE = {
    Fault.PARTIAL: "다음 주기에 실패분만 재시도합니다",
    Fault.NETWORK_OFFLINE: "와이파이·VPN 연결을 확인하세요. 연결되면 저절로 재개됩니다",
    Fault.NETWORK_UNSTABLE: "연결이 불안정합니다. 다음 주기에 재시도합니다",
    Fault.SERVICE_DOWN: "Dooray 서버 문제입니다. 계속되면 기관 담당자에게 문의하세요",
    Fault.SERVICE_ERROR: "Dooray가 요청을 거부했습니다. 로그의 resultCode를 확인하세요",
    Fault.RATE_LIMITED: "요청이 몰렸습니다. 주기를 자동으로 늘려 물러납니다",
    Fault.AUTH: "토큰이 만료·회수됐을 수 있습니다. 설치.bat으로 토큰을 다시 등록하세요",
    Fault.LOCAL: "파일이 잠겼거나 권한이 없습니다. 해당 파일을 닫고 다시 실행하세요",
    Fault.CONFIG: "설정을 고친 뒤 그 폴더에서 synchere.bat을 한 번 실행하세요",
    Fault.HELD: "폴더에서 직접 실행해 계획을 확인하세요",
}

# 기다리면 저절로 풀리는 것들 — 사람을 부르지 않는다.
_TRANSIENT = frozenset({
    Fault.PARTIAL, Fault.NETWORK_OFFLINE, Fault.NETWORK_UNSTABLE,
    Fault.SERVICE_DOWN, Fault.RATE_LIMITED,
})

# DNS 해석 실패. Windows는 WSAHOST_NOT_FOUND(11001)로 온다 — 와이파이가 끊기면
# 거의 항상 이 모양이다(실측 2026-08-16 15:26, 그리고 08-15 reconcile 110건).
_OFFLINE_MARKS = ("getaddrinfo", "11001", "name or service not known",
                  "temporary failure in name resolution")


def classify(exc: BaseException | None, *, exit_code: int | None = None) -> str:
    """예외(+종료코드)를 Fault로. 예외가 없으면 종료코드만으로 판정한다."""
    if exc is None:
        return {0: Fault.OK, 1: Fault.PARTIAL, 2: Fault.CONFIG,
                4: Fault.HELD}.get(exit_code or 0, Fault.UNKNOWN)

    from .client import DoorayApiError

    if isinstance(exc, OSError) and not isinstance(exc, DoorayApiError):
        return Fault.LOCAL

    if isinstance(exc, DoorayApiError):
        if exc.status == 429:
            return Fault.RATE_LIMITED
        if exc.status in (401, 403):
            return Fault.AUTH
        if exc.status is not None and 500 <= exc.status < 600:
            return Fault.SERVICE_DOWN
        if exc.result_code is not None and exc.result_code < 0:
            # HTTP 200인데 envelope이 실패 — 서버가 요청 자체를 거부했다(R15).
            return Fault.SERVICE_ERROR
        cause = exc.cause
        if cause is not None:
            text = f"{type(cause).__name__}: {cause}".lower()
            if any(mark in text for mark in _OFFLINE_MARKS):
                return Fault.NETWORK_OFFLINE
            return Fault.NETWORK_UNSTABLE
        if exc.status is None:
            # 전송 원인이 안 실린 경우 — 메시지로 최소 판정만 한다.
            text = str(exc).lower()
            if any(mark in text for mark in _OFFLINE_MARKS):
                return Fault.NETWORK_OFFLINE
            if "네트워크" in str(exc):
                return Fault.NETWORK_UNSTABLE
        return Fault.UNKNOWN

    name = type(exc).__name__
    if name in ("TokenNotFound",):
        return Fault.AUTH
    return Fault.UNKNOWN


def is_transient(fault: str) -> bool:
    """기다리면 풀리는가. 사람을 부를지 말지의 기준."""
    return fault in _TRANSIENT
