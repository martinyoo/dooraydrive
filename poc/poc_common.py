"""Dooray Drive API PoC 공통 기반.

- 토큰: 환경변수 DOORAY_API_TOKEN → keyring("dooray-sync", "api-token") 순으로 탐색
- 모든 응답의 X-RateLimit-* 헤더를 자동 수집
- 307 리다이렉트는 자동 추적하지 않고(follow_redirects=False) 수동 처리하여
  Location 호스트·Authorization 유지 동작을 관측/제어한다
- 결과는 poc_results/<이름>.json + <이름>.log 에 기록
- 안전 수칙: 쓰기 실험은 개인 드라이브 "_poc_sandbox" 폴더 안에서만.
  영구삭제(DELETE) API는 이 모듈에 존재하지 않는다(의도적).
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

# Windows 콘솔 cp949 대응 — import 시점에 즉시 적용
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import httpx
except ImportError:
    print("httpx가 필요합니다:  pip install -r requirements.txt")
    sys.exit(1)

BASE_URL = os.environ.get("DOORAY_BASE_URL", "https://api.gov-dooray.com").rstrip("/")
SANDBOX_NAME = "_poc_sandbox"
POC_DIR = Path(__file__).resolve().parent
RESULTS_DIR = POC_DIR / "poc_results"
TMP_DIR = POC_DIR / "tmp"
CONTEXT_PATH = RESULTS_DIR / "context.json"


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def load_token() -> str:
    token = os.environ.get("DOORAY_API_TOKEN", "").strip()
    if token:
        return token
    try:
        import keyring  # optional

        token = keyring.get_password("dooray-sync", "api-token") or ""
    except Exception:
        token = ""
    if token:
        return token
    print(
        "API 토큰을 찾을 수 없습니다.\n"
        "  1) Dooray 웹 → 개인설정 > API > 개인 인증 토큰 에서 발급\n"
        "  2) PowerShell:  $env:DOORAY_API_TOKEN = '발급받은토큰'\n"
        "     또는 keyring: python -c \"import keyring;keyring.set_password('dooray-sync','api-token','토큰')\""
    )
    sys.exit(2)


def load_context() -> dict:
    if CONTEXT_PATH.exists():
        return json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
    return {}


def update_context(**kw) -> dict:
    ctx = load_context()
    ctx.update(kw)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_PATH.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return ctx


def file_digests(data: bytes) -> dict:
    """hash 알고리즘 식별용: 3종 해시를 hex/base64 두 인코딩으로 계산."""
    out = {}
    for algo in ("md5", "sha1", "sha256"):
        d = hashlib.new(algo, data).digest()
        out[f"{algo}_hex"] = d.hex()
        out[f"{algo}_b64"] = base64.b64encode(d).decode()
    return out


def match_hash(remote_hash: str | None, digests: dict) -> str | None:
    """원격 hash 문자열이 어떤 알고리즘/인코딩과 일치하는지 판별."""
    if not remote_hash:
        return None
    rh = remote_hash.strip()
    for key, val in digests.items():
        if rh.lower() == val.lower():
            return key
    return None


class PocClient:
    """PoC 전용 Dooray API 클라이언트 (rate-limit 관측 + 수동 307 처리)."""

    def __init__(self, name: str):
        self.name = name
        self.token = load_token()
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        self._logf = open(RESULTS_DIR / f"{name}.log", "a", encoding="utf-8")
        self.client = httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=1800.0, pool=10.0),
            headers={"Authorization": f"dooray-api {self.token}"},
        )
        self.ratelimit_samples: list[dict] = []
        self.results: dict = {"started_at": now_iso(), "base_url": BASE_URL}
        self.log(f"=== {name} 시작 ({now_iso()}) base_url={BASE_URL}")

    # ---------- 로깅/기록 ----------
    def log(self, msg: str):
        line = f"[{now_iso()}] {msg}"
        print(line)
        self._logf.write(line + "\n")
        self._logf.flush()

    def save(self):
        self.results["finished_at"] = now_iso()
        self.results["ratelimit_samples_tail"] = self.ratelimit_samples[-20:]
        out = RESULTS_DIR / f"{self.name}.json"
        out.write_text(json.dumps(self.results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.log(f"결과 저장: {out}")

    def _capture_ratelimit(self, resp: httpx.Response, label: str):
        sample = {
            "t": now_iso(),
            "label": label,
            "status": resp.status_code,
            "remaining": resp.headers.get("X-RateLimit-Remaining"),
            "requested": resp.headers.get("X-RateLimit-Requested-Tokens"),
            "burst": resp.headers.get("X-RateLimit-Burst-Capacity"),
            "replenish": resp.headers.get("X-RateLimit-Replenish-Rate"),
        }
        self.ratelimit_samples.append(sample)
        return sample

    # ---------- HTTP ----------
    def request(self, method: str, url: str, *, retry429: bool = True, label: str = "", **kw) -> httpx.Response:
        """url은 절대 URL 또는 BASE_URL 기준 경로. 429는 기본 3회 재시도."""
        full = url if url.startswith("http") else BASE_URL + url
        for attempt in range(4):
            resp = self.client.request(method, full, **kw)
            self._capture_ratelimit(resp, label or f"{method} {url}")
            if resp.status_code == 429 and retry429 and attempt < 3:
                delay = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
                self.log(f"429 수신 → {delay:.1f}s 대기 후 재시도 ({attempt + 1}/3)")
                time.sleep(delay)
                continue
            return resp
        return resp

    def api(self, method: str, path: str, *, expect_success: bool = True, **kw) -> dict:
        """envelope(header/result) 해석. 실패 시 예외."""
        resp = self.request(method, path, **kw)
        try:
            body = resp.json()
        except Exception:
            raise RuntimeError(f"{method} {path} → HTTP {resp.status_code}, JSON 아님: {resp.text[:300]}")
        header = body.get("header", {})
        if expect_success and not (resp.status_code == 200 and header.get("isSuccessful")):
            raise RuntimeError(
                f"{method} {path} → HTTP {resp.status_code}, "
                f"resultCode={header.get('resultCode')}, resultMessage={header.get('resultMessage')}"
            )
        return body

    # ---------- 파일 전송 (수동 307) ----------
    def download_raw(self, drive_id: str, file_id: str, dest: Path) -> dict:
        """?media=raw 다운로드. 307 수동 추적 + 임시파일 + os.replace 원자 교체."""
        info: dict = {"file_id": file_id}
        path = f"/drive/v1/drives/{drive_id}/files/{file_id}?media=raw"
        r1 = self.request("GET", path, label="download-first-hop")
        info["first_status"] = r1.status_code
        if r1.status_code in (301, 302, 303, 307):
            loc = r1.headers.get("location", "")
            info["redirect_status"] = r1.status_code
            info["location_host"] = httpx.URL(loc).host if loc else None
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            written = 0
            # 같은 client 사용 → Authorization 기본 헤더가 file-api 호스트에도 포함됨
            with self.client.stream("GET", loc) as r2:
                self._capture_ratelimit(r2, "download-second-hop")
                info["second_status"] = r2.status_code
                info["content_length"] = r2.headers.get("content-length")
                if r2.status_code != 200:
                    r2.read()
                    raise RuntimeError(f"다운로드 2차 요청 실패 HTTP {r2.status_code}: {r2.text[:300]}")
                with open(tmp, "wb") as f:
                    for chunk in r2.iter_bytes(1024 * 256):
                        f.write(chunk)
                        written += len(chunk)
            os.replace(tmp, dest)  # 원자 교체
            info["bytes_written"] = written
        elif r1.status_code == 200:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r1.content)
            info["bytes_written"] = len(r1.content)
            info["note"] = "307 없이 직접 응답"
        else:
            raise RuntimeError(f"다운로드 1차 요청 실패 HTTP {r1.status_code}: {r1.text[:300]}")
        return info

    def _transfer_multipart(self, method: str, path: str, filename: str, data: bytes) -> tuple[dict, dict]:
        """POST/PUT multipart 업로드의 수동 307 처리. (info, envelope) 반환."""
        info: dict = {"filename": filename, "size": len(data)}
        r1 = self.request(method, path, files={"file": (filename, data)}, label="upload-first-hop")
        info["first_status"] = r1.status_code
        if r1.status_code in (301, 302, 303, 307):
            loc = r1.headers.get("location", "")
            info["redirect_status"] = r1.status_code
            info["location_host"] = httpx.URL(loc).host if loc else None
            # 307 재요청: multipart body를 새로 구성해야 함
            r2 = self.request(method, loc, files={"file": (filename, data)}, label="upload-second-hop")
            info["second_status"] = r2.status_code
            final = r2
        else:
            final = r1
        try:
            body = final.json()
        except Exception:
            raise RuntimeError(f"업로드 응답이 JSON 아님 HTTP {final.status_code}: {final.text[:300]}")
        header = body.get("header", {})
        if not (final.status_code == 200 and header.get("isSuccessful")):
            raise RuntimeError(
                f"업로드 실패 HTTP {final.status_code}, resultCode={header.get('resultCode')}, "
                f"resultMessage={header.get('resultMessage')}"
            )
        return info, body

    def upload_new(self, drive_id: str, parent_id: str, filename: str, data: bytes) -> tuple[dict, dict]:
        return self._transfer_multipart(
            "POST", f"/drive/v1/drives/{drive_id}/files?parentId={parent_id}", filename, data
        )

    def upload_version(self, drive_id: str, file_id: str, filename: str, data: bytes) -> tuple[dict, dict]:
        return self._transfer_multipart(
            "PUT", f"/drive/v1/drives/{drive_id}/files/{file_id}?media=raw", filename, data
        )

    # ---------- Drive 조작 헬퍼 (안전: 영구삭제 없음) ----------
    def list_children(self, drive_id: str, parent_id: str | None = None, page: int = 0, size: int = 100, **params) -> dict:
        p = {"page": page, "size": size, **params}
        if parent_id is not None:
            p["parentId"] = parent_id
        return self.api("GET", f"/drive/v1/drives/{drive_id}/files", params=p)

    def file_meta(self, drive_id: str, file_id: str) -> dict:
        return self.api("GET", f"/drive/v1/drives/{drive_id}/files/{file_id}", params={"media": "meta"})

    def create_folder(self, drive_id: str, parent_folder_id: str, name: str) -> str:
        body = self.api("POST", f"/drive/v1/drives/{drive_id}/files/{parent_folder_id}/create-folder", json={"name": name})
        return body["result"]["id"]

    def rename(self, drive_id: str, file_id: str, new_name: str):
        return self.api("PUT", f"/drive/v1/drives/{drive_id}/files/{file_id}?media=meta", json={"name": new_name})

    def move(self, drive_id: str, file_id: str, destination_file_id: str):
        return self.api("POST", f"/drive/v1/drives/{drive_id}/files/{file_id}/move", json={"destinationFileId": destination_file_id})

    def move_to_trash(self, drive_id: str, file_id: str):
        """PoC의 유일한 '삭제' — 휴지통 이동만. 영구삭제 API는 구현하지 않음."""
        return self.move(drive_id, file_id, "trash")

    def changes(self, drive_id: str, *, param_name: str = "latestRevision", value=None, file_id=None, size: int = 200) -> dict:
        params: dict = {"size": size}
        if value is not None:
            params[param_name] = value
        if file_id is not None:
            params["fileId"] = file_id
        return self.api("GET", f"/drive/v2/drives/{drive_id}/changes", params=params)

    # ---------- 샌드박스 ----------
    def find_root_folder(self, drive_id: str) -> str:
        body = self.list_children(drive_id, None, type="folder", subTypes="root")
        roots = [f for f in body["result"] if f.get("subType") == "root"]
        if not roots:
            raise RuntimeError("root 폴더를 찾지 못했습니다: " + json.dumps(body["result"])[:300])
        return roots[0]["id"]

    def find_child_by_name(self, drive_id: str, parent_id: str, name: str) -> dict | None:
        page = 0
        target = unicodedata.normalize("NFC", name)
        while True:
            body = self.list_children(drive_id, parent_id, page=page, size=100)
            items = body.get("result") or []
            for it in items:
                if unicodedata.normalize("NFC", it.get("name") or "") == target:
                    return it
            if len(items) < 100:
                return None
            page += 1

    def ensure_sandbox(self, drive_id: str) -> str:
        """대상 드라이브 root 아래 _poc_sandbox 폴더를 찾거나 생성.

        캐시된 sandbox_id를 신뢰하지 않고 매번 이름으로 재탐색한다 —
        드라이브 전환·수동 정리(휴지통 이동) 시 낡은 id로 쓰기가 나가는 것을 방지
        (안전성 > API 호출 2회 절약).
        """
        root_id = self.find_root_folder(drive_id)
        existing = self.find_child_by_name(drive_id, root_id, SANDBOX_NAME)
        if existing:
            sandbox_id = existing["id"]
        else:
            sandbox_id = self.create_folder(drive_id, root_id, SANDBOX_NAME)
            self.log(f"샌드박스 폴더 생성: {SANDBOX_NAME} ({sandbox_id})")
        update_context(drive_id=drive_id, root_id=root_id, sandbox_id=sandbox_id)
        return sandbox_id

    def close(self):
        self.client.close()
        self._logf.close()


def require_drive_id() -> str:
    drive_id = os.environ.get("DOORAY_DRIVE_ID") or load_context().get("drive_id")
    if not drive_id:
        print("drive_id가 없습니다. 먼저 poc_01_auth_drives.py 를 실행하세요.")
        sys.exit(2)
    return drive_id
