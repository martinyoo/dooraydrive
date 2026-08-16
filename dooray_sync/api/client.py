"""Dooray API HTTP 단일 관문 (규약 §7).

이 모듈 바깥에서는 httpx를 직접 호출하지 않는다. 실측이 강제한 규칙 A1~A4를
한 곳에 모아 두어야 감사(audit)와 회귀 테스트가 가능하기 때문이다.

- **A1 envelope 검사**: 오류가 HTTP 200으로 온다. 실측(PoC-09, 8건 관측):
  이미 휴지통에 있는 항목의 move가 `HTTP 200 + resultCode=-15700100
  ("No access authority")`로 실패했다. `status == 200`과 `header.isSuccessful`이
  **둘 다** 참일 때만 성공으로 판정한다(규약 §12-2).
- **A2 307 수동 처리**: `follow_redirects=False` 고정. 실측(03_download):
  1차 요청이 307로 `file-api.gov-dooray.com`을 가리키고, 재요청에 Authorization을
  붙이지 않으면 401이다. 업로드 body는 재생할 수 없으므로 재요청 시 파일을
  **재오픈**해 multipart를 새로 만든다.
- **A3 선제 감속**: 응답의 X-RateLimit-* 를 매번 기록하고 Remaining이
  LOW_WATERMARK 미만이면 1/replenish_rate 만큼 쉬어 간다. 429는 Retry-After 우선.
  파일 객체 업로드는 스트림이 소진되므로 자동 재시도를 금지한다.
- **A4 타임아웃**: connect 10 / read 600 / write 1800.
  (changes 쿼리 응답이 요청당 ~4.8초, 100MB 업로드 실측 근거)

토큰은 로그·예외 메시지 어디에도 남기지 않는다(규약 §12-5). 307 Location에는
서명이 들어갈 수 있으므로 로그에는 **호스트만** 기록한다.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

# SSL 검사(중간 프록시)를 하는 망에서는 서버 인증서 체인에 기관 자체 서명 루트 CA가
# 끼어든다. 그 CA는 Windows 인증서 저장소에는 있지만 certifi 번들에는 없으므로,
# httpx 기본 설정으로는 모든 요청이 CERTIFICATE_VERIFY_FAILED → ConnectError로
# 죽는다(실측 2026-08-10, gov-dooray). truststore를 ssl에 주입하면 OS 저장소를
# 그대로 쓴다 — 검증을 끄는 것이 아니다. 위치가 여기인 이유: 이 모듈이 모든 HTTP의
# 단일 관문이라(위 docstring), CLI 진입점만이 아니라 sync_here·discover_roots처럼
# in-process로 API를 쓰는 경로도 전부 지나간다.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass  # 미설치 PC는 certifi 기본 동작 — SSL 검사가 없는 망에서는 문제없다

from ..util.paths import ext_path

__all__ = ["DoorayApiError", "DoorayClient"]

# GET(다운로드)은 메서드가 바뀌어도 무해하므로 3xx 전반을 따라간다.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# 업로드는 메서드 보존이 보장되는 것만 따라간다 — 302/303을 POST로 재시도하는 것은
# HTTP 위반이고, 서버가 표현을 바꾸면 조용히 오동작하느니 멈추는 편이 안전하다.
_METHOD_PRESERVING_REDIRECTS = frozenset({307, 308})

_MAX_RETRIES = 3          # 429/5xx·네트워크 오류 재시도 상한 (2, 4, 6초)
_MAX_BACKOFF_SEC = 60.0   # Retry-After가 비상식적으로 커도 여기서 자른다
_BACKOFF_BASE = 2.0       # 네트워크 오류 백오프 기준(초)
_DOWNLOAD_CHUNK = 1024 * 256   # PoC-03/06에서 사용한 값
_ERR_BODY_LIMIT = 300     # 예외 메시지에 실을 응답 본문 길이

# 재시도할 전송 계층 오류. 실측: 긴 순회 중 서버가 연결을 끊어
# RemoteProtocolError("incomplete chunked read")가 발생한다(TransportError의 하위).
# httpx.TransportError는 연결·타임아웃·프로토콜 오류를 모두 포괄한다.
_TRANSIENT_NETWORK_ERRORS = (httpx.TransportError,)


def _new_md5():
    # util.hashing._new_md5와 같은 이유(FIPS 모드 회피). 다운로드는 스트리밍으로
    # 해시를 함께 계산해야 해서 파일을 다시 읽는 md5_file을 쓸 수 없다.
    try:
        return hashlib.md5(usedforsecurity=False)
    except TypeError:
        return hashlib.md5()


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _body_excerpt(resp: httpx.Response) -> str:
    """오류 진단용 본문 일부. 스트리밍 응답이면 먼저 읽어 온다."""
    try:
        if not resp.is_closed and not getattr(resp, "is_stream_consumed", False):
            resp.read()
        return resp.text[:_ERR_BODY_LIMIT]
    except Exception:
        return "<본문 읽기 실패>"


class DoorayApiError(RuntimeError):
    """API 실패. HTTP 실패와 envelope 실패(A1)를 같은 타입으로 올린다.

    `result_code`를 별도로 보존하는 이유: 호출측이 특정 코드를 관용 처리해야 한다.
    실측 -15700100은 '이미 휴지통에 있는 항목의 move'에서 나오며,
    이 경우 호출측은 '이미 처리됨'으로 넘긴다(규약 §8 move_to_trash).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        result_code: int | None = None,
        result_message: str | None = None,
        path: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.result_code = result_code
        self.result_message = result_message
        self.path = path
        # 전송 계층 원인(ConnectError/ReadTimeout/RemoteProtocolError…).
        # 메시지 문자열을 다시 파싱해 원인을 추측하지 않기 위해 객체를 들고 있는다.
        self.cause = cause


class DoorayClient:
    """실측 강제 규칙 A1~A4의 단일 관문."""

    LOW_WATERMARK = 5

    def __init__(self, base_url: str, token: str, *, logger: logging.Logger | None = None) -> None:
        if not base_url or not str(base_url).strip():
            raise ValueError("base_url이 비어 있습니다")
        # 토큰 값 자체는 어떤 메시지에도 넣지 않는다.
        if not token or not str(token).strip():
            raise ValueError("API 토큰이 비어 있습니다")

        self.base_url = str(base_url).strip().rstrip("/")
        self._token = str(token).strip()
        self.logger = logger or logging.getLogger("dooray_sync.api")

        self._client = httpx.Client(
            follow_redirects=False,  # A2: 307을 수동 추적한다
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=1800.0, pool=10.0),
            headers={"Authorization": f"dooray-api {self._token}"},
        )
        # A3 관측값. doctor 명령이 그대로 출력한다.
        self.last_rate_limit: dict[str, str | None] = {
            "remaining": None,
            "burst": None,
            "replenish": None,
            "requested": None,
        }
        # M3 관측 카운터 — 기존 감속·재시도 로직은 그대로 두고 횟수만 센다.
        # sync --report-json이 싣고, 무인 러너가 이 값으로 다음 주기를 조정한다
        # (429 관측 → 주기 x2, 깨끗한 실행 → x0.75 감쇠. 설계 §3.5 적응형 승수).
        self.counters: dict[str, int] = {
            "rate_limited": 0,     # 429 응답 수신 횟수
            "pace_events": 0,      # 선제 감속(_pace)이 실제로 쉰 횟수
            "http_retries": 0,     # 429/5xx로 재시도한 횟수
            "network_retries": 0,  # 전송 계층 오류로 재시도한 횟수
        }

    # ------------------------------------------------------------------
    # 수명 주기
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DoorayClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        """A2: 리다이렉트 재요청에 Authorization을 명시적으로 재부착.

        httpx Client의 기본 헤더로도 붙지만, 실측 근거(무인증 재요청 401)가 있는
        요구사항이라 코드에 드러나게 둔다.
        """
        return {"Authorization": f"dooray-api {self._token}"}

    def _resolve(self, url: str) -> str:
        u = str(url or "")
        if u.startswith("http://") or u.startswith("https://"):
            return u
        return self.base_url + (u if u.startswith("/") else "/" + u)

    def _capture_rate_limit(self, resp: httpx.Response, label: str) -> None:
        h = resp.headers
        if resp.status_code == 429:
            self.counters["rate_limited"] += 1
        self.last_rate_limit = {
            "remaining": h.get("X-RateLimit-Remaining"),
            "burst": h.get("X-RateLimit-Burst-Capacity"),
            "replenish": h.get("X-RateLimit-Replenish-Rate"),
            "requested": h.get("X-RateLimit-Requested-Tokens"),
        }
        self.logger.debug(
            "%s → HTTP %s (remaining=%s burst=%s replenish=%s)",
            label,
            resp.status_code,
            self.last_rate_limit["remaining"],
            self.last_rate_limit["burst"],
            self.last_rate_limit["replenish"],
        )

    def _pace(self) -> None:
        """A3 선제 감속. 429를 맞고 나서 물러서는 것보다 미리 쉬는 쪽이 싸다.

        실측: burst=20, replenish=5/s (전 실행 일관). 남은 토큰이 바닥에 가까우면
        토큰 1개가 다시 차는 시간(1/replenish)만큼 쉰다.
        """
        remaining = _int_or_none(self.last_rate_limit.get("remaining"))
        if remaining is None or remaining >= self.LOW_WATERMARK:
            return
        rate = _int_or_none(self.last_rate_limit.get("replenish")) or 5
        if rate <= 0:
            rate = 5
        delay = 1.0 / rate
        self.counters["pace_events"] += 1
        self.logger.debug("rate-limit 선제 감속: remaining=%s → %.2fs 대기", remaining, delay)
        time.sleep(delay)

    @staticmethod
    def _should_retry(status: int) -> bool:
        return status == 429 or 500 <= status < 600

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        """429는 Retry-After 우선, 없으면 2/4/6초 (규약 §7 A3)."""
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, min(float(str(raw).strip()), _MAX_BACKOFF_SEC))
            except ValueError:
                # HTTP-date 형식이면 파싱하지 않고 기본 백오프로 떨어진다.
                pass
        return min(2.0 * (attempt + 1), _MAX_BACKOFF_SEC)

    @contextlib.contextmanager
    def _stream(self, method: str, url: str, *, label: str, **kw) -> Iterator[httpx.Response]:
        """스트리밍 요청 1회. 감속·헤더 기록을 포함하되 재시도는 하지 않는다
        (이미 바이트를 쓰기 시작한 전송은 되돌릴 수 없다)."""
        self._pace()
        with self._client.stream(method, url, **kw) as resp:
            self._capture_rate_limit(resp, label)
            yield resp

    def _write_stream(self, resp: httpx.Response, dest_tmp: Path) -> tuple[int, str]:
        """본문을 dest_tmp에 쓰면서 MD5를 함께 계산. (바이트 수, md5hex) 반환."""
        parent = Path(dest_tmp).parent
        os.makedirs(ext_path(parent), exist_ok=True)
        h = _new_md5()
        written = 0
        # 규약 §12-4: 로컬 파일은 반드시 \\?\ 경유로 연다.
        with open(ext_path(dest_tmp), "wb") as f:
            for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                f.write(chunk)
                h.update(chunk)
                written += len(chunk)
            # os.replace는 메타데이터 원자성만 보장한다(NTFS는 파일 데이터를 저널링하지
            # 않는다). 여기서 디스크까지 밀어두지 않으면 정전 시 dest는 존재하는데
            # 내용이 잘려 있고, 원본은 이미 회수된 뒤가 된다.
            f.flush()
            os.fsync(f.fileno())
        return written, h.hexdigest()

    def _envelope(self, resp: httpx.Response, method: str, path: str) -> dict:
        """A1: 성공 판정은 status와 header.isSuccessful의 논리곱으로만 한다."""
        try:
            body = resp.json()
        except Exception:
            raise DoorayApiError(
                f"{method} {path} → HTTP {resp.status_code}, JSON 응답이 아님: "
                f"{_body_excerpt(resp)}",
                status=resp.status_code,
                path=path,
            ) from None
        if not isinstance(body, dict):
            raise DoorayApiError(
                f"{method} {path} → HTTP {resp.status_code}, 예상과 다른 응답 형식",
                status=resp.status_code,
                path=path,
            )

        header = body.get("header")
        if not isinstance(header, dict):
            header = {}
        result_code = _int_or_none(header.get("resultCode"))
        result_message = header.get("resultMessage")
        if not isinstance(result_message, str):
            result_message = None if result_message is None else str(result_message)

        # isSuccessful이 참(bool True)이 아니면 전부 실패로 본다. 관대하게 해석하면
        # 실측 -15700100 같은 200-실패를 성공으로 오인한다.
        if resp.status_code != 200 or header.get("isSuccessful") is not True:
            raise DoorayApiError(
                f"{method} {path} → HTTP {resp.status_code}, "
                f"resultCode={result_code}, resultMessage={result_message}",
                status=resp.status_code,
                result_code=result_code,
                result_message=result_message,
                path=path,
            )
        return body

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    def request(self, method: str, url: str, *, retry: bool = True, label: str = "", **kw) -> httpx.Response:
        """url이 절대면 그대로, 아니면 base_url + path. follow_redirects=False 고정.

        429/5xx **와 전송 계층 오류**를 retry=True일 때 재시도한다. 실측: 이 API는
        긴 순회 중 연결을 끊는 일이 있고(RemoteProtocolError: incomplete chunked read),
        재시도가 없으면 그 한 번에 동기화 전체가 실패한다. 재택 회선에서는 흔한 상황이다.
        파일 객체를 실어 보내는 요청은 스트림이 이미 소진되어 재전송이 불가능하므로
        반드시 retry=False로 부른다.
        """
        full = self._resolve(url)
        lbl = label or f"{method} {url}"
        attempts = _MAX_RETRIES if retry else 0
        resp: httpx.Response | None = None
        for attempt in range(attempts + 1):
            self._pace()
            try:
                resp = self._client.request(method, full, **kw)
            except _TRANSIENT_NETWORK_ERRORS as exc:
                if attempt >= attempts:
                    raise DoorayApiError(
                        f"{lbl} 네트워크 오류로 실패({attempts + 1}회 시도): "
                        f"{type(exc).__name__}: {exc}",
                        path=url,
                        cause=exc,
                    ) from exc
                delay = _BACKOFF_BASE * (attempt + 1)
                self.counters["network_retries"] += 1
                self.logger.warning(
                    "%s 네트워크 오류(%s) → %.1fs 대기 후 재시도 (%d/%d)",
                    lbl, type(exc).__name__, delay, attempt + 1, attempts,
                )
                time.sleep(delay)
                continue
            self._capture_rate_limit(resp, lbl)
            if attempt < attempts and self._should_retry(resp.status_code):
                delay = self._retry_delay(resp, attempt)
                self.counters["http_retries"] += 1
                self.logger.warning(
                    "%s HTTP %s → %.1fs 대기 후 재시도 (%d/%d)",
                    lbl, resp.status_code, delay, attempt + 1, attempts,
                )
                time.sleep(delay)
                continue
            return resp
        assert resp is not None  # 도달 불가 — 루프는 최소 1회 실행된다
        return resp

    def api(self, method: str, path: str, **kw) -> dict:
        """envelope 검사(A1) 후 body dict 반환. 실패 시 DoorayApiError."""
        label = kw.pop("label", "") or f"{method} {path}"
        resp = self.request(method, path, label=label, **kw)
        return self._envelope(resp, method, path)

    def download_to(self, path: str, dest_tmp: Path) -> dict:
        """?media=raw GET. 307 수동 추적(A2) + 스트리밍으로 dest_tmp에 기록.

        반환: {'bytes', 'md5', 'content_length', 'redirect_host'}
        ※ 검증과 원자적 교체(os.replace)는 호출측(DriveAPI.download) 책임이다.
        """
        first_url = self._resolve(path)
        dest_tmp = Path(dest_tmp)

        target_url: str | None = None
        redirect_host: str | None = None

        with self._stream("GET", first_url, label="download-first-hop") as r1:
            if r1.status_code in _REDIRECT_STATUSES:
                loc = r1.headers.get("location", "")
                if not loc:
                    raise DoorayApiError(
                        f"다운로드 리다이렉트에 Location이 없습니다 (HTTP {r1.status_code})",
                        status=r1.status_code,
                        path=path,
                    )
                # 상대 Location도 안전하게 흡수한다.
                target_url = str(httpx.URL(first_url).join(loc))
                redirect_host = httpx.URL(target_url).host
            elif r1.status_code == 200:
                # 실측상 항상 307이지만, 서버가 직접 응답하는 경우도 처리해 둔다.
                written, md5 = self._write_stream(r1, dest_tmp)
                return {
                    "bytes": written,
                    "md5": md5,
                    "content_length": _int_or_none(r1.headers.get("content-length")),
                    "redirect_host": None,
                }
            else:
                raise DoorayApiError(
                    f"다운로드 1차 요청 실패 HTTP {r1.status_code}: {_body_excerpt(r1)}",
                    status=r1.status_code,
                    path=path,
                )

        # A2: 재요청에 Authorization을 재부착한다(무인증이면 401 — 실측 03_download).
        # Location 전문에는 서명이 들어갈 수 있어 로그에는 호스트만 남긴다.
        self.logger.debug("다운로드 307 → host=%s", redirect_host)
        with self._stream(
            "GET", target_url, label="download-second-hop", headers=self._auth_headers()
        ) as r2:
            if r2.status_code != 200:
                raise DoorayApiError(
                    f"다운로드 2차 요청 실패 HTTP {r2.status_code} (host={redirect_host}): "
                    f"{_body_excerpt(r2)}",
                    status=r2.status_code,
                    path=path,
                )
            content_length = _int_or_none(r2.headers.get("content-length"))
            written, md5 = self._write_stream(r2, dest_tmp)

        return {
            "bytes": written,
            "md5": md5,
            "content_length": content_length,
            "redirect_host": redirect_host,
        }

    def upload_file(self, method: str, path: str, filename: str, local_path: Path) -> dict:
        """multipart 업로드(POST 신규 / PUT 새 버전). envelope 검사 후 body 반환.

        A2: 307을 받으면 **파일을 재오픈**해 multipart를 다시 만든다 — 1차 요청에서
        파일 객체가 이미 소진되어 그대로는 재전송할 수 없다.
        A3: retry=False 고정. 같은 이유로 자동 재시도를 붙이면 빈 body가 나간다.
        """
        local_path = Path(local_path)
        first_path = path

        # 규약 §12-4: 로컬 파일은 \\?\ 경유로만 연다.
        with open(ext_path(local_path), "rb") as f:
            r1 = self.request(
                method, first_path, retry=False, label="upload-first-hop",
                files={"file": (filename, f)},
            )

        final = r1
        if r1.status_code in _REDIRECT_STATUSES:
            if r1.status_code not in _METHOD_PRESERVING_REDIRECTS:
                # 302/303은 메서드를 GET으로 바꾸라는 뜻이라 업로드를 그대로 재전송하면 안 된다.
                raise DoorayApiError(
                    f"업로드가 메서드 비보존 리다이렉트(HTTP {r1.status_code})를 받았습니다 "
                    "— 안전을 위해 중단합니다",
                    status=r1.status_code,
                    path=path,
                )
            loc = r1.headers.get("location", "")
            if not loc:
                raise DoorayApiError(
                    f"업로드 리다이렉트에 Location이 없습니다 (HTTP {r1.status_code})",
                    status=r1.status_code,
                    path=path,
                )
            target_url = str(httpx.URL(self._resolve(first_path)).join(loc))
            self.logger.debug("업로드 307 → host=%s", httpx.URL(target_url).host)
            with open(ext_path(local_path), "rb") as f2:
                final = self.request(
                    method, target_url, retry=False, label="upload-second-hop",
                    headers=self._auth_headers(),
                    files={"file": (filename, f2)},
                )

        return self._envelope(final, method, path)
