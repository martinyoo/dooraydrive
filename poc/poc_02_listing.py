"""PoC-02: 파일 목록 API 순회.

검증 항목
- 페이지네이션 동작 (totalCount 유무, 종료 조건)
- 목록 응답에 hash 필드가 있는지 (없으면 원격 변경 감지는 version/revision 기반으로 확정)
- 응답 필드 실측 (revision, subType 분포, root/trash 표현)
- _poc_sandbox 폴더 생성 (이후 PoC의 격리 공간)

실행: python poc_02_listing.py   (선행: poc_01)
"""
from collections import Counter

from poc_common import PocClient, require_drive_id

MAX_FILES = 500  # 순회 상한 (개인 드라이브 전체를 다 읽지 않도록)

pc = PocClient("02_listing")
try:
    drive_id = require_drive_id()

    # 1) root 폴더 식별 (type=folder&subTypes=root)
    body = pc.list_children(drive_id, None, type="folder", subTypes="root,trash")
    pc.results["root_trash_listing"] = body.get("result")
    root_id = pc.find_root_folder(drive_id)
    pc.results["root_id"] = root_id
    pc.log(f"root 폴더 id={root_id}")

    # 2) BFS 순회 (상한 MAX_FILES)
    seen = []
    field_presence = Counter()
    subtypes = Counter()
    pagination_note = {}
    queue = [(root_id, "/")]
    while queue and len(seen) < MAX_FILES:
        folder_id, path = queue.pop(0)
        page = 0
        while len(seen) < MAX_FILES:
            resp = pc.list_children(drive_id, folder_id, page=page, size=100)
            items = resp.get("result") or []
            if page == 0 and "totalCount" not in pagination_note:
                pagination_note["totalCount_present"] = "totalCount" in resp
                pagination_note["totalCount_value_example"] = resp.get("totalCount")
            for it in items:
                seen.append({"path": path + (it.get("name") or "?"), "type": it.get("type"), "id": it.get("id")})
                subtypes[f"{it.get('type')}/{it.get('subType')}"] += 1
                for key in ("hash", "revision", "version", "size", "mimeType", "updatedAt", "parentFile"):
                    if key in it:
                        field_presence[key + "_present"] += 1
                    if it.get(key) is not None:
                        field_presence[key + "_nonnull"] += 1
                if it.get("type") == "folder" and it.get("subType") not in ("trash",):
                    queue.append((it["id"], path + (it.get("name") or "?") + "/"))
            if len(items) < 100:
                break
            page += 1

    pc.results["files_seen_count"] = len(seen)
    pc.results["files_sample"] = seen[:30]
    pc.results["field_presence"] = dict(field_presence)
    pc.results["subtype_distribution"] = dict(subtypes)
    pc.results["pagination"] = pagination_note
    pc.results["hash_in_listing"] = field_presence.get("hash_nonnull", 0) > 0
    pc.log(f"순회 파일 수: {len(seen)} (상한 {MAX_FILES})")
    pc.log(f"목록 응답에 hash 존재: {pc.results['hash_in_listing']}  ← 설계 결정 포인트")
    pc.log(f"필드 존재 빈도: {dict(field_presence)}")
    pc.log(f"subType 분포: {dict(subtypes)}")

    # 3) 샌드박스 폴더 준비
    sandbox_id = pc.ensure_sandbox(drive_id)
    pc.results["sandbox_id"] = sandbox_id
    pc.log(f"샌드박스 준비 완료: {sandbox_id}")

    pc.save()
finally:
    pc.close()
