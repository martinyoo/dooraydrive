"""PoC-05: changes API 의미론 실측 — 델타 동기화 설계 전체가 여기에 걸림.

검증 항목
1. 파라미터 실명: latestRevision= vs revision= (문서 불일치) — 어느 쪽이 필터로 동작하는지
2. 커서 의미: 기준 revision 자체가 포함(inclusive)인지 제외(exclusive)인지, fileId 병용 의미
3. 초기 전체 스캔 용도: revision=0부터 전량 페이징이 가능한지 (초기 동기화 전략)
4. 시나리오별 표현: 생성/수정/개명/폴더생성/이동/폴더개명(하위 path 반영?)/휴지통 이동
5. hash 알고리즘: MD5/SHA-1/SHA-256 × hex/base64 대조
6. revision 단조 증가 여부

실행: python poc_05_changes.py   (선행: poc_01, poc_02)
"""
import os
import time

from poc_common import PocClient, file_digests, match_hash, require_drive_id

PAGE_CAP = 200  # 초기 스캔 상한 (실측: 이 드라이브의 누적 이력이 13,799건 ≈ 71페이지)

pc = PocClient("05_changes")


def rev_key(entry):
    """revision을 정렬 가능한 값으로 (숫자 문자열 가정, 아니면 원본)."""
    r = entry.get("revision")
    try:
        return int(r)
    except (TypeError, ValueError):
        return r


def scan_all(param_name: str, keep_entries: bool = True):
    """revision 0부터 live tip 까지 전량 페이징.

    [중요·실측] changes API는 size 보다 적은 부분 페이지를 반환하면서도 뒤에 더 많은
    데이터가 남아있다. 따라서 `len(items) < size` 를 종료 조건으로 쓰면 안 되고,
    반드시 **0건 응답**까지 페이징해야 한다. (이 조건을 잘못 잡아 1차 실행이 실패했음)
    반환: (entries, pages, capped, last_rev)
    """
    entries, cursor_rev, cursor_fid, pages = [], 0, None, 0
    while pages < PAGE_CAP:
        body = pc.changes(drive_id, param_name=param_name, value=cursor_rev, file_id=cursor_fid, size=200)
        items = body.get("result") or []
        pages += 1
        if not items:
            break  # 0건 = live tip 도달 (유일한 정상 종료 조건)
        if keep_entries:
            entries.extend(items)
        last = items[-1]
        new_rev, new_fid = last.get("revision"), (last.get("file") or {}).get("id")
        if (new_rev, new_fid) == (cursor_rev, cursor_fid):
            break  # 커서 미전진 → 무한루프 방지
        cursor_rev, cursor_fid = new_rev, new_fid
    return entries, pages, pages >= PAGE_CAP, cursor_rev


def drain_from(cursor_rev, param_name=None, max_pages=30):
    """cursor_rev 이후 도착한 모든 델타를 0건 응답(live tip)까지 끌어온다.

    반환: (수집된 항목들, 전진한 최종 cursor)
    """
    param_name = param_name or working_param
    collected, cursor_fid, pages = [], None, 0
    while pages < max_pages:
        body = pc.changes(drive_id, param_name=param_name, value=cursor_rev, file_id=cursor_fid, size=200)
        items = body.get("result") or []
        pages += 1
        if not items:
            break
        collected.extend(items)
        last = items[-1]
        new_rev, new_fid = last.get("revision"), (last.get("file") or {}).get("id")
        if (new_rev, new_fid) == (cursor_rev, cursor_fid):
            break
        cursor_rev, cursor_fid = new_rev, new_fid
    return collected, cursor_rev


def poll_changes(since_rev, pred, timeout=150, param_name=None):
    """since_rev 이후 델타를 live tip 까지 끌어오며 pred 만족 항목을 기다린다.

    실측 반영 지연은 1~2초(사실상 즉시)이나, 여유를 두고 timeout 150초를 기본값으로 유지.
    (주의: 과거 주석의 "30~90초 지연"은 부분 페이지 버그로 인한 오측이었음 — 검토보고서 §3.2)
    반환: (매칭 항목, 이 창에서 본 전체 항목, 전진한 cursor)
    """
    deadline = time.time() + timeout
    seen, cursor = [], since_rev
    while True:
        batch, cursor = drain_from(cursor, param_name)
        seen.extend(batch)
        hits = [e for e in seen if pred(e)]
        if hits or time.time() >= deadline:
            return hits, seen, cursor
        time.sleep(5)


try:
    drive_id = require_drive_id()
    sandbox_id = pc.ensure_sandbox(drive_id)

    # ── 1. 파라미터 실명 판별 ──────────────────────────────────────────────
    # 마커 파일을 하나 올린 뒤, "매우 큰 revision 값"으로 두 파라미터를 각각 호출:
    # 필터로 동작하는 파라미터라면 결과가 0건(또는 마커 이후만), 무시된다면 처음부터 전부 반환된다.
    marker_payload = os.urandom(8 * 1024)
    _, marker_body = pc.upload_new(drive_id, sandbox_id, "poc05_marker.bin", marker_payload)
    marker_id = marker_body["result"]["id"]
    marker_rev = marker_body["result"].get("revision")
    pc.log(f"마커 업로드 id={marker_id} revision={marker_rev}")

    HUGE = 10**15
    probe = {}
    for pname in ("latestRevision", "revision"):
        body = pc.changes(drive_id, param_name=pname, value=HUGE, size=5)
        probe[pname] = {"count": len(body.get("result") or []), "sample": (body.get("result") or [])[:2]}
        pc.log(f"파라미터 프로브 {pname}={HUGE} → {probe[pname]['count']}건")
    pc.results["param_probe"] = probe
    # 필터로 동작하는(=0건을 돌려주는) 파라미터를 채택
    if probe["latestRevision"]["count"] == 0:
        working_param = "latestRevision"
    elif probe["revision"]["count"] == 0:
        working_param = "revision"
    else:
        working_param = "latestRevision"  # 판별 불가 시 문서 표기 우선, 결과에 기록
        pc.log("[경고] 두 파라미터 모두 필터로 동작하지 않는 것으로 보임 — 원시 응답 확인 필요")
    pc.results["working_param"] = working_param
    pc.log(f"채택 파라미터: {working_param}")

    # ── 2. 초기 전체 스캔 (revision=0 → live tip) ────────────────────────
    all_entries, pages, capped, tip_rev = scan_all(working_param)
    revs = [rev_key(e) for e in all_entries]
    pc.results["full_scan"] = {
        "entries": len(all_entries),
        "pages": pages,
        "capped": capped,
        "ordering_ascending": revs == sorted(revs) if revs and all(isinstance(r, int) for r in revs) else None,
        "has_folder_entries": any((e.get("file") or {}).get("type") == "folder" for e in all_entries),
        "changetype_distribution": {},
        "sample_first_2": all_entries[:2],
        "sample_last_2": all_entries[-2:],
    }
    from collections import Counter

    pc.results["full_scan"]["changetype_distribution"] = dict(Counter(e.get("changeType") for e in all_entries))
    marker_found = any((e.get("file") or {}).get("id") == marker_id for e in all_entries)
    pc.results["full_scan"]["marker_found"] = marker_found
    pc.log(
        f"전체 스캔: {len(all_entries)}건/{pages}페이지 capped={capped} "
        f"오름차순={pc.results['full_scan']['ordering_ascending']} 마커포함={marker_found}"
    )

    # baseline 은 반드시 live tip (0건 응답 지점) 이어야 한다.
    # 수집분의 max(revision) 을 쓰면 부분 페이지에서 조기 종료된 값이 섞일 수 있다.
    baseline = tip_rev
    pc.results["baseline_revision"] = baseline
    pc.results["tip_reached"] = not capped
    pc.log(f"live tip revision={baseline} (capped={capped})")

    # 커서 inclusive/exclusive 판별: baseline(마지막 항목의 revision)으로 조회 시 그 항목이 다시 오는가
    body = pc.changes(drive_id, param_name=working_param, value=baseline, size=10)
    boundary = body.get("result") or []
    pc.results["cursor_inclusive"] = any(rev_key(e) == baseline for e in boundary)
    pc.log(f"커서 경계: baseline 항목 재포함(inclusive)={pc.results['cursor_inclusive']}")

    # ── 3. 시나리오 단계별 표현 실측 ─────────────────────────────────────
    scenarios = {}
    cursor = baseline

    def run_step(name, action, pred):
        global cursor
        action_result = action()
        hits, window, new_cursor = poll_changes(cursor, pred)
        scenarios[name] = {
            "hits": hits,
            "window_size": len(window),
            "cursor_before": cursor,
            "cursor_after": new_cursor,
        }
        pc.log(f"[{name}] 변경항목 {len(hits)}건 감지 (창 {len(window)}건), cursor {cursor}→{new_cursor}")
        for h in hits[:3]:
            f = h.get("file") or {}
            pc.log(f"    changeType={h.get('changeType')} type={f.get('type')} name={f.get('name')!r} path={f.get('path')!r} ver={f.get('version')} hash={str(f.get('hash'))[:20]}")
        cursor = new_cursor
        return action_result, hits

    c1 = os.urandom(64 * 1024)
    d1 = file_digests(c1)

    # (a) 파일 생성 — upload_new는 (info, envelope) 튜플 반환
    (_, up_body), hits = run_step(
        "a_create",
        lambda: pc.upload_new(drive_id, sandbox_id, "poc05_A.bin", c1),
        lambda e: (e.get("file") or {}).get("name") == "poc05_A.bin",
    )
    a_id = up_body["result"]["id"]

    # hash 알고리즘 판별
    remote_hash = None
    for h in hits:
        f = h.get("file") or {}
        if f.get("name") == "poc05_A.bin":
            remote_hash = f.get("hash")
    algo = match_hash(remote_hash, d1)
    pc.results["hash_algorithm"] = {"remote_hash": remote_hash, "matched": algo, "local_digests": d1}
    pc.log(f"hash 알고리즘 판별: {algo or '불일치(원시값 기록됨)'}  ← 설계 결정 포인트")

    # (b) 내용 수정 (PUT 새 버전)
    c2 = os.urandom(80 * 1024)
    d2 = file_digests(c2)
    _, hits_b = run_step(
        "b_modify",
        lambda: pc.upload_version(drive_id, a_id, "poc05_A.bin", c2),
        lambda e: (e.get("file") or {}).get("id") == a_id,
    )
    rh2 = next(((e.get("file") or {}).get("hash") for e in hits_b if (e.get("file") or {}).get("id") == a_id), None)
    pc.results["hash_after_modify"] = {"remote": rh2, "matched": match_hash(rh2, d2)}

    # (c) 이름 변경
    run_step(
        "c_rename",
        lambda: pc.rename(drive_id, a_id, "poc05_A_renamed.bin"),
        lambda e: (e.get("file") or {}).get("id") == a_id,
    )

    # (d) 폴더 생성 — 폴더가 changes에 나타나는가
    f_id_holder = {}

    def make_folder():
        f_id_holder["id"] = pc.create_folder(drive_id, sandbox_id, "poc05_folder")
        return f_id_holder["id"]

    run_step("d_mkdir", make_folder, lambda e: (e.get("file") or {}).get("name") == "poc05_folder")
    f_id = f_id_holder["id"]

    # (e) 파일을 폴더로 이동 — path 변경으로 나타나는가
    run_step("e_move_file", lambda: pc.move(drive_id, a_id, f_id), lambda e: (e.get("file") or {}).get("id") == a_id)

    # (f) 폴더 이름 변경 — 하위 파일의 path 변경 항목이 생기는가 (핵심!)
    _, hits_f = run_step(
        "f_rename_folder",
        lambda: pc.rename(drive_id, f_id, "poc05_folder_renamed"),
        lambda e: (e.get("file") or {}).get("id") in (f_id, a_id),
    )
    child_path_change = any((e.get("file") or {}).get("id") == a_id for e in hits_f)
    # 폴더 개명 후 잠시 더 기다리며 하위 파일 항목 확인 (반영 지연 고려)
    if not child_path_change:
        extra, _, _ = poll_changes(
            scenarios["f_rename_folder"]["cursor_before"],
            lambda e: (e.get("file") or {}).get("id") == a_id,
            timeout=60,
        )
        child_path_change = bool(extra)
    pc.results["folder_rename_propagates_child_path"] = child_path_change
    pc.log(f"폴더 개명 시 하위 파일 path 변경 항목 발생: {child_path_change}  ← R8 결정 포인트")

    # (g) 휴지통 이동 — deleted 로 오는가, updated 로 오는가
    _, hits_g = run_step("g_trash_file", lambda: pc.move_to_trash(drive_id, a_id), lambda e: (e.get("file") or {}).get("id") == a_id)
    pc.results["trash_representation"] = [{"changeType": e.get("changeType"), "file": e.get("file")} for e in hits_g]

    # (h) 폴더 휴지통 이동
    run_step("h_trash_folder", lambda: pc.move_to_trash(drive_id, f_id), lambda e: (e.get("file") or {}).get("id") == f_id)

    # 마커 정리
    pc.move_to_trash(drive_id, marker_id)

    # ── 4. 단조성/커서 검증 ───────────────────────────────────────────────
    all_revs = []
    for s in scenarios.values():
        all_revs.extend(rev_key(e) for e in s["hits"] if isinstance(rev_key(e), int))
    pc.results["revision_monotonic_nondecreasing"] = all_revs == sorted(all_revs)
    pc.results["scenarios"] = scenarios
    pc.log(f"revision 단조성(시나리오 순서 기준): {pc.results['revision_monotonic_nondecreasing']}")

    pc.log("PoC-05 완료 — 결과 JSON을 검토보고서에 반영할 것")
    pc.save()
finally:
    pc.close()
