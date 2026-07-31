"""PoC-01: 인증 + 드라이브 목록.

검증 항목
- 개인 API 토큰 인증 동작 (GET /common/v1/members/me)
- 드라이브 목록 조회 (개인/프로젝트)
- IP ACL 차단 여부 (이 스크립트가 실행되는 망에서의 접근성) — 실패 시 프로젝트 no-go
- 사용할 drive_id를 context.json에 저장

실행: python poc_01_auth_drives.py
"""
import os
import sys

from poc_common import PocClient, update_context

pc = PocClient("01_auth_drives")
try:
    # 1) 인증 확인 — 가장 단순한 자기 정보 조회
    try:
        me = pc.api("GET", "/common/v1/members/me")
        pc.results["members_me"] = me.get("result")
        pc.log(f"인증 성공: organizationMemberId={ (me.get('result') or {}).get('id') or (me.get('result') or {}) }")
    except Exception as e:
        pc.results["members_me_error"] = str(e)
        pc.log(f"[중요] /common/v1/members/me 실패 — 토큰 문제 또는 IP ACL 차단 가능성: {e}")
        pc.log("이 망에서 API 접근이 차단된 경우, 기관 Dooray 관리자에게 ACL 정책 확인 필요 (검토보고서 R4)")
        pc.save()
        sys.exit(1)

    # 2) 드라이브 목록 3종
    for label, params in [
        ("private", {"type": "private"}),
        ("project_private", {"type": "project", "scope": "private"}),
        ("project_public", {"type": "project", "scope": "public"}),
    ]:
        try:
            body = pc.api("GET", "/drive/v1/drives", params=params)
            drives = body.get("result") or []
            pc.results[f"drives_{label}"] = drives
            pc.results[f"drives_{label}_totalCount"] = body.get("totalCount")
            pc.log(f"드라이브({label}): {len(drives)}개")
            for d in drives[:10]:
                pc.log(f"  - id={d.get('id')} name={d.get('name')} type={d.get('type')}")
        except Exception as e:
            pc.results[f"drives_{label}_error"] = str(e)
            pc.log(f"드라이브({label}) 조회 실패: {e}")

    # 3) 사용할 드라이브 선택: 환경변수 우선, 아니면 개인 드라이브
    drive_id = os.environ.get("DOORAY_DRIVE_ID", "").strip()
    if not drive_id:
        privates = pc.results.get("drives_private") or []
        if not privates:
            pc.log("개인 드라이브가 없습니다. DOORAY_DRIVE_ID 환경변수로 대상 드라이브를 지정하세요.")
            pc.save()
            sys.exit(1)
        drive_id = privates[0]["id"]
    pc.results["selected_drive_id"] = drive_id
    from poc_common import load_context

    if load_context().get("drive_id") != drive_id:
        # 드라이브가 바뀌면 이전 드라이브의 root/sandbox id를 함께 무효화 (낡은 id로 쓰기 방지)
        update_context(drive_id=drive_id, root_id=None, sandbox_id=None)
    else:
        update_context(drive_id=drive_id)
    pc.log(f"선택된 drive_id={drive_id} → context.json 저장")

    # 4) 드라이브 상세
    try:
        detail = pc.api("GET", f"/drive/v1/drives/{drive_id}")
        pc.results["drive_detail"] = detail.get("result")
        pc.log(f"드라이브 상세 조회 성공 (members 필드 포함 여부: {'members' in (detail.get('result') or {})})")
    except Exception as e:
        pc.results["drive_detail_error"] = str(e)
        pc.log(f"드라이브 상세 조회 실패: {e}")

    # 5) rate-limit 헤더 존재 확인
    last = pc.ratelimit_samples[-1] if pc.ratelimit_samples else {}
    pc.results["ratelimit_headers_present"] = bool(last.get("remaining"))
    pc.log(f"rate-limit 헤더: remaining={last.get('remaining')} burst={last.get('burst')} replenish={last.get('replenish')}")

    pc.log("PoC-01 완료: 인증/접근성 GREEN")
    pc.save()
finally:
    pc.close()
