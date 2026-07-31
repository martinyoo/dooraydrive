"""PoC-09: 재검증에서 남은 미확정 항목 실측 (Fable 5 감사 결과 반영).

검증 항목 (모두 poc_results/09_net_semantics.json + .log 에 근거 보존)
1. coalescing: 폴링 없이 생성→수정→개명을 연속 수행한 뒤 단일 조회 시
   changes 항목이 1건(최종 상태)인지 3건(개별 이벤트)인지 — net-뷰 가설의 직접 검증
2. 부모 폴더 updated 이벤트 발생 조건: 자식 파일 "생성" 시에도 부모 폴더가
   changes에 나타나는지 (이전 관측은 "휴지통 이동" 시에만 확인됨)
3. 업로드 409 의미론 재현: 동일 폴더/동일 이름 즉시 재업로드 시 409 여부,
   다른 이름+동일 내용 업로드 시 허용 여부 (이전엔 stdout만 출력, 미보존)
4. 목록 API 엄격 페이지네이션: totalCount보다 작은 size로 여러 번 조회해
   부분 페이지 없이 정확히 반환되는지 (요청 수만큼 정확 반환 여부)
5. changes 응답 원시 envelope 1건 보존 (totalCount 필드 존재 여부 확인용)
6. 확장자 없는 예약어("CON") 업로드 시 서버 수용 여부

안전 수칙: 전부 _poc_sandbox 내부에서만. 영구삭제 API 미사용(휴지통 이동만).
"""
import os
import time

from poc_common import PocClient, file_digests, require_drive_id

pc = PocClient("09_net_semantics")
try:
    drive_id = require_drive_id()
    sandbox_id = pc.ensure_sandbox(drive_id)
    cleanup_ids = []

    # ── 0. changes 원시 envelope 1건 보존 ──────────────────────────────
    raw = pc.request("GET", f"/drive/v2/drives/{drive_id}/changes", params={"latestRevision": 0, "size": 3})
    pc.results["raw_envelope"] = raw.json()
    pc.results["raw_envelope_has_totalCount"] = "totalCount" in raw.json()
    pc.log(f"changes 원시 envelope 최상위 키: {list(raw.json().keys())}")

    # ── 1. coalescing: 폴링 없이 생성→수정→개명 연속 수행 ──────────────
    # 시작 커서 확보 (live tip)
    def tip_cursor():
        cur, fid = 0, None
        while True:
            body = pc.changes(drive_id, param_name="latestRevision", value=cur, file_id=fid, size=200)
            items = body.get("result") or []
            if not items:
                return cur
            last = items[-1]
            new_cur, new_fid = last.get("revision"), (last.get("file") or {}).get("id")
            if (new_cur, new_fid) == (cur, fid):
                return cur
            cur, fid = new_cur, new_fid

    start_cursor = tip_cursor()
    pc.log(f"coalescing 실험 시작 커서(tip)={start_cursor}")

    payload1 = os.urandom(4096)
    _, up_body = pc.upload_new(drive_id, sandbox_id, "poc09_coalesce.bin", payload1)
    c_id = up_body["result"]["id"]
    cleanup_ids.append(c_id)

    payload2 = os.urandom(8192)
    pc.upload_version(drive_id, c_id, "poc09_coalesce.bin", payload2)

    pc.rename(drive_id, c_id, "poc09_coalesce_renamed.bin")

    # 폴링 없이 짧게 1회 대기 후 단일 조회 (net-뷰라면 최종 상태 1건, 이벤트로그라면 3건 근접)
    time.sleep(3)
    body = pc.changes(drive_id, param_name="latestRevision", value=start_cursor, size=200)
    items = body.get("result") or []
    coalesce_hits = [e for e in items if (e.get("file") or {}).get("id") == c_id]
    pc.results["coalescing"] = {
        "start_cursor": start_cursor,
        "total_window_items": len(items),
        "hits_for_target_id": len(coalesce_hits),
        "hits": coalesce_hits,
    }
    pc.log(f"coalescing: 대상 id에 대한 항목 수 = {len(coalesce_hits)}건 (1건=net-뷰 가설 지지, 3건=이벤트로그 가설 지지)")
    for h in coalesce_hits:
        f = h.get("file") or {}
        pc.log(f"  changeType={h.get('changeType')} name={f.get('name')!r} version={f.get('version')} hash={str(f.get('hash'))[:16]}")

    # ── 2. 부모 폴더 updated 이벤트: 자식 "생성" 시에도 발생하는가 ──────
    folder_start_cursor = tip_cursor()
    subfolder_id = pc.create_folder(drive_id, sandbox_id, "poc09_parent_test")
    cleanup_ids.append(subfolder_id)
    time.sleep(1)
    body = pc.changes(drive_id, param_name="latestRevision", value=folder_start_cursor, size=200)
    items = body.get("result") or []
    parent_hit_on_mkdir = any((e.get("file") or {}).get("id") == subfolder_id for e in items)

    cursor2 = None
    for e in items:
        r = e.get("revision")
        try:
            cursor2 = max(cursor2 or 0, int(r))
        except (TypeError, ValueError):
            pass
    cursor2 = cursor2 or folder_start_cursor

    _, child_body = pc.upload_new(drive_id, subfolder_id, "poc09_child.bin", os.urandom(2048))
    child_id = child_body["result"]["id"]
    cleanup_ids.append(child_id)
    time.sleep(2)
    body2 = pc.changes(drive_id, param_name="latestRevision", value=cursor2, size=200)
    items2 = body2.get("result") or []
    parent_hit_on_child_create = any((e.get("file") or {}).get("id") == subfolder_id for e in items2)
    child_hit = any((e.get("file") or {}).get("id") == child_id for e in items2)
    pc.results["parent_folder_event"] = {
        "on_mkdir_itself": parent_hit_on_mkdir,
        "on_child_create__parent_appears": parent_hit_on_child_create,
        "on_child_create__child_appears": child_hit,
        "window_after_child_create": [
            {"changeType": e.get("changeType"), "file": e.get("file")} for e in items2
        ],
    }
    pc.log(f"부모폴더 이벤트: 폴더생성 자체={parent_hit_on_mkdir}, 자식생성 시 부모 재등장={parent_hit_on_child_create}, 자식 자체 등장={child_hit}")

    # ── 3. 업로드 409 의미론 재현 (결과 보존) ──────────────────────────
    name409 = "poc09_409test.bin"
    _, b1 = pc.upload_new(drive_id, sandbox_id, name409, os.urandom(1024))
    id409 = b1["result"]["id"]
    cleanup_ids.append(id409)

    result_409 = {}
    try:
        _, b2 = pc.upload_new(drive_id, sandbox_id, name409, os.urandom(2048))
        result_409["immediate_same_name_diff_content"] = {"outcome": "accepted", "id": b2["result"]["id"]}
        cleanup_ids.append(b2["result"]["id"])
    except Exception as e:
        result_409["immediate_same_name_diff_content"] = {"outcome": "rejected", "error": str(e)[:200]}

    time.sleep(15)
    try:
        _, b3 = pc.upload_new(drive_id, sandbox_id, name409, os.urandom(3072))
        result_409["after_15s_same_name_diff_content"] = {"outcome": "accepted", "id": b3["result"]["id"]}
        cleanup_ids.append(b3["result"]["id"])
    except Exception as e:
        result_409["after_15s_same_name_diff_content"] = {"outcome": "rejected", "error": str(e)[:200]}

    same_content = os.urandom(1500)
    _, b4 = pc.upload_new(drive_id, sandbox_id, "poc09_409_nameA.bin", same_content)
    cleanup_ids.append(b4["result"]["id"])
    try:
        _, b5 = pc.upload_new(drive_id, sandbox_id, "poc09_409_nameB.bin", same_content)
        result_409["diff_name_same_content"] = {"outcome": "accepted", "id": b5["result"]["id"]}
        cleanup_ids.append(b5["result"]["id"])
    except Exception as e:
        result_409["diff_name_same_content"] = {"outcome": "rejected", "error": str(e)[:200]}

    pc.results["upload_409_semantics"] = result_409
    pc.log(f"409 의미론: {result_409}")

    # ── 4. 목록 API 엄격 페이지네이션 (totalCount 대비 부분 페이지 검사) ──
    pag_folder_id = pc.create_folder(drive_id, sandbox_id, "poc09_pagination_test")
    cleanup_ids.append(pag_folder_id)
    N = 7
    child_ids = []
    for i in range(N):
        _, b = pc.upload_new(drive_id, pag_folder_id, f"poc09_pg_{i:02d}.bin", os.urandom(256))
        child_ids.append(b["result"]["id"])
    cleanup_ids.extend(child_ids)

    pagination_check = {}
    for size in (2, 3, 5, N, N + 5):
        body = pc.list_children(drive_id, pag_folder_id, page=0, size=size)
        items = body.get("result") or []
        pagination_check[f"size={size}"] = {
            "returned": len(items),
            "totalCount": body.get("totalCount"),
            "expected_min": min(size, N),
            "matches_expected": len(items) == min(size, N),
        }
    pc.results["listing_pagination_strict"] = pagination_check
    all_match = all(v["matches_expected"] for v in pagination_check.values())
    pc.results["listing_pagination_no_partial_page"] = all_match
    pc.log(f"목록 API 엄격 페이지네이션(자식 {N}개): {pagination_check}")
    pc.log(f"목록 API 부분페이지 없음 확정 여부: {all_match}")

    # ── 5. 확장자 없는 예약어("CON") 업로드 ────────────────────────────
    reserved_result = {}
    try:
        _, br = pc.upload_new(drive_id, sandbox_id, "CON", os.urandom(512))
        reserved_result = {"accepted": True, "stored_name": br["result"].get("name")}
        cleanup_ids.append(br["result"]["id"])
    except Exception as e:
        reserved_result = {"accepted": False, "error": str(e)[:200]}
    pc.results["reserved_name_no_extension"] = reserved_result
    pc.log(f"확장자 없는 'CON' 업로드: {reserved_result}")

    # ── 정리 ────────────────────────────────────────────────────────
    for fid in cleanup_ids:
        try:
            pc.move_to_trash(drive_id, fid)
        except Exception as e:
            pc.log(f"휴지통 이동 실패 id={fid}: {e}")
    pc.log(f"정리 완료: {len(cleanup_ids)}개 휴지통 이동")

    pc.save()
finally:
    pc.close()
