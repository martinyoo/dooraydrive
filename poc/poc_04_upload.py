"""PoC-04: 업로드(신규 POST / 새 버전 PUT) 307 흐름.

검증 항목
- POST /files?parentId= 의 307 흐름 (Location 호스트, multipart 재전송)
- 한글 파일명 multipart 인코딩 왕복 (업로드명 == 메타 조회명, NFC 기준)
- PUT ?media=raw 새 버전 업로드 → version 증가 확인
- 동일 이름 재업로드(POST) 시 서버 동작 (409? 중복 생성? 자동 개명?)

실행: python poc_04_upload.py   (선행: poc_01, poc_02)
"""
import os
import unicodedata

from poc_common import PocClient, require_drive_id

KOREAN_NAME = "한글 테스트 파일 (PoC).txt"

pc = PocClient("04_upload")
try:
    drive_id = require_drive_id()
    sandbox_id = pc.ensure_sandbox(drive_id)

    # 1) 신규 업로드 (한글 파일명, 100KB)
    payload_v1 = os.urandom(100 * 1024)
    up_info, up_body = pc.upload_new(drive_id, sandbox_id, KOREAN_NAME, payload_v1)
    result = up_body["result"]
    file_id = result["id"]
    pc.results["post_upload"] = {"info": up_info, "result": result}
    pc.log(f"POST 업로드: id={file_id}, 응답 필드={sorted(result.keys())}")
    pc.log(f"  1차 HTTP {up_info.get('first_status')} → Location 호스트={up_info.get('location_host')} → 2차 HTTP {up_info.get('second_status')}")

    # 2) 메타 조회로 파일명 왕복 확인
    meta = pc.file_meta(drive_id, file_id)["result"]
    remote_name = meta.get("name") or ""
    same_nfc = unicodedata.normalize("NFC", remote_name) == unicodedata.normalize("NFC", KOREAN_NAME)
    pc.results["korean_name_roundtrip"] = {
        "sent": KOREAN_NAME,
        "received": remote_name,
        "match_nfc": same_nfc,
        "received_is_nfc": remote_name == unicodedata.normalize("NFC", remote_name),
    }
    pc.log(f"한글 파일명 왕복: {'PASS' if same_nfc else 'FAIL'} (서버 저장명: {remote_name!r})")
    pc.results["meta_after_post"] = meta

    # 3) 새 버전 업로드 (PUT ?media=raw)
    payload_v2 = os.urandom(120 * 1024)
    put_info, put_body = pc.upload_version(drive_id, file_id, KOREAN_NAME, payload_v2)
    pc.results["put_upload"] = {"info": put_info, "result": put_body.get("result")}
    pc.log(f"PUT 새 버전: 응답={put_body.get('result')}")

    meta2 = pc.file_meta(drive_id, file_id)["result"]
    pc.results["meta_after_put"] = meta2
    pc.results["version_increment"] = {"before": meta.get("version"), "after": meta2.get("version")}
    pc.log(f"version: {meta.get('version')} → {meta2.get('version')}, size: {meta.get('size')} → {meta2.get('size')}")

    # 4) 동일 이름 재업로드(POST) — 중복 처리 방식 실측
    try:
        dup_info, dup_body = pc.upload_new(drive_id, sandbox_id, KOREAN_NAME, os.urandom(10 * 1024))
        dup_result = dup_body["result"]
        pc.results["duplicate_post"] = {"behavior": "created", "result": dup_result, "info": dup_info}
        pc.log(f"동일 이름 POST → 새 파일 생성됨 id={dup_result.get('id')} name={dup_result.get('name')!r} (중복 허용 or 자동개명)")
        pc.move_to_trash(drive_id, dup_result["id"])
    except Exception as e:
        pc.results["duplicate_post"] = {"behavior": "rejected", "error": str(e)}
        pc.log(f"동일 이름 POST → 거부됨: {e}")

    # 5) 정리
    pc.move_to_trash(drive_id, file_id)
    pc.log("정리 완료 (휴지통 이동)")

    pc.save()
finally:
    pc.close()
