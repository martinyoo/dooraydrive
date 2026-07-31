"""PoC-03: 다운로드 307 흐름.

검증 항목
- ?media=raw 1차 요청의 응답 코드(307 기대)와 Location의 실제 공공 클라우드 file-api 호스트
- 2차 요청에 Authorization이 필요한지 (무인증 시도 → 인증 시도 비교)
- 스트리밍 다운로드 + 임시파일 + os.replace 원자 교체
- 바이트 무결성 (업로드 원본과 해시 대조)

실행: python poc_03_download.py   (선행: poc_01, poc_02)
"""
import os

import httpx

from poc_common import BASE_URL, PocClient, TMP_DIR, file_digests, require_drive_id

pc = PocClient("03_download")
try:
    drive_id = require_drive_id()
    sandbox_id = pc.ensure_sandbox(drive_id)

    # 1) 테스트 파일 업로드 (1MB 랜덤)
    payload = os.urandom(1024 * 1024)
    digests = file_digests(payload)
    up_info, up_body = pc.upload_new(drive_id, sandbox_id, "poc03_download_test.bin", payload)
    file_id = up_body["result"]["id"]
    pc.results["upload_info"] = up_info
    pc.results["file_id"] = file_id
    pc.log(f"테스트 파일 업로드 완료 id={file_id}")

    # 2) 1차 요청을 수동 관찰: 307 + Location 확인
    path = f"/drive/v1/drives/{drive_id}/files/{file_id}?media=raw"
    r1 = pc.request("GET", path, label="dl-first-hop-observe")
    loc = r1.headers.get("location", "")
    pc.results["first_hop"] = {
        "status": r1.status_code,
        "location_host": httpx.URL(loc).host if loc else None,
        "location_path_prefix": str(httpx.URL(loc).path)[:60] if loc else None,
    }
    pc.log(f"1차 응답: HTTP {r1.status_code}, Location 호스트={pc.results['first_hop']['location_host']}")

    # 3) 2차 요청 무인증 시도 → Authorization 필요 여부 실측
    if loc:
        with httpx.Client(follow_redirects=False, timeout=60) as bare:
            r_noauth = bare.get(loc)
        pc.results["second_hop_without_auth_status"] = r_noauth.status_code
        pc.log(f"2차 요청(무인증): HTTP {r_noauth.status_code}  ← 401이면 Authorization 재부착 필수 확정")

    # 4) 정식 다운로드 (공통 함수: 307 수동 추적 + 원자 교체)
    dest = TMP_DIR / "poc03_downloaded.bin"
    dl_info = pc.download_raw(drive_id, file_id, dest)
    pc.results["download_info"] = dl_info

    # 5) 무결성 검증
    downloaded = dest.read_bytes()
    ok = file_digests(downloaded)["sha256_hex"] == digests["sha256_hex"]
    pc.results["integrity_ok"] = ok
    pc.results["size_match"] = len(downloaded) == len(payload)
    pc.log(f"무결성 검증: {'PASS' if ok else 'FAIL'} (size {len(downloaded)}/{len(payload)})")

    # 6) 정리: 원격 테스트 파일은 휴지통으로, 로컬 임시파일 삭제
    pc.move_to_trash(drive_id, file_id)
    dest.unlink(missing_ok=True)
    pc.log("정리 완료 (원격: 휴지통 이동 / 로컬: 임시파일 삭제)")

    pc.save()
finally:
    pc.close()
