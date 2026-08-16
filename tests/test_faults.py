"""오류 분류 — "무엇이 고장났고 사람이 무엇을 해야 하는가".

무인 실행에는 화면을 보고 판단하는 사람이 없다. 종료코드 1 하나에 와이파이
끊김·Dooray 장애·일부 파일 실패가 전부 들어 있으면, 러너는 셋을 똑같이
다룰 수밖에 없다 — 사람이 할 일은 각각 기다린다/문의한다/아무것도 안 한다인데.
"""
from __future__ import annotations

import httpx

from dooray_sync.api.client import DoorayApiError
from dooray_sync.api.faults import Fault, classify, is_transient


def test_wifi_drop_is_offline():
    """2026-08-16 15:26 실측: 와이파이가 끊기면 DNS 해석이 먼저 죽는다.

    Windows는 WSAHOST_NOT_FOUND(11001)로 온다. 이걸 일반 네트워크 오류와
    묶으면 '와이파이 확인하세요'라는 정확한 안내를 못 한다.
    """
    cause = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    exc = DoorayApiError("GET /drive/v1/... 네트워크 오류로 실패(4회 시도)",
                         path="/drive/v1/x", cause=cause)
    assert classify(exc) == Fault.NETWORK_OFFLINE
    assert is_transient(Fault.NETWORK_OFFLINE)      # 사람을 부르지 않는다


def test_connection_reset_is_unstable():
    """연결은 되는데 전송이 끊기는 것 — 이름 해석은 됐으므로 회선 문제다."""
    cause = httpx.RemoteProtocolError("peer closed connection")
    exc = DoorayApiError("네트워크 오류로 실패", cause=cause)
    assert classify(exc) == Fault.NETWORK_UNSTABLE


def test_timeout_is_unstable():
    exc = DoorayApiError("네트워크 오류로 실패", cause=httpx.ReadTimeout("timed out"))
    assert classify(exc) == Fault.NETWORK_UNSTABLE


def test_5xx_is_service_down():
    assert classify(DoorayApiError("서버 오류", status=503)) == Fault.SERVICE_DOWN
    assert is_transient(Fault.SERVICE_DOWN)


def test_429_is_rate_limited():
    assert classify(DoorayApiError("한도", status=429)) == Fault.RATE_LIMITED


def test_401_403_is_auth():
    """토큰 만료·회수·권한 — 기다려도 안 풀린다. 즉시 사람을 불러야 한다."""
    for status in (401, 403):
        assert classify(DoorayApiError("거부", status=status)) == Fault.AUTH
    assert not is_transient(Fault.AUTH)


def test_negative_result_code_is_service_error():
    """R15: HTTP 200인데 envelope이 실패한 경우(실측 -15700100)."""
    exc = DoorayApiError("거부", status=200, result_code=-15700100)
    assert classify(exc) == Fault.SERVICE_ERROR
    assert not is_transient(Fault.SERVICE_ERROR)


def test_os_error_is_local():
    assert classify(PermissionError(13, "Permission denied")) == Fault.LOCAL


def test_exit_codes_without_exception():
    assert classify(None, exit_code=0) == Fault.OK
    assert classify(None, exit_code=1) == Fault.PARTIAL
    assert classify(None, exit_code=2) == Fault.CONFIG
    assert classify(None, exit_code=4) == Fault.HELD


def test_partial_is_transient_not_failure():
    """exit 1은 고장이 아니다(I-A11) — 정상 운용에서 늘 생기고 다음 주기가 재시도한다."""
    assert is_transient(Fault.PARTIAL)


def test_runner_prefers_report_fault_over_exit_code():
    """러너의 판정은 보고의 fault가 정본이고 종료코드는 폴백이다."""
    from dooray_sync.auto.runner import _classify

    kind, msg = _classify(1, {"fault": Fault.NETWORK_OFFLINE})
    assert kind == Fault.NETWORK_OFFLINE
    assert "와이파이" in msg

    # 보고가 없으면(자식이 쓰기 전에 죽었으면) 종료코드로 떨어진다
    kind2, _msg2 = _classify(1, {})
    assert kind2 == Fault.PARTIAL
