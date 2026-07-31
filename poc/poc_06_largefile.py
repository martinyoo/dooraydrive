"""PoC-06: 대용량 파일 업/다운로드.

검증 항목
- 대용량(기본 100MB) 업로드/다운로드 소요 시간, 성공 여부, API 측 크기 제한 실측
- 업로드 중단(프로세스 kill) 후 서버 상태: 쓰레기(부분) 파일이 남는가
- 재시도(전체 재전송) 성공 여부

주의: 스트리밍 업로드를 위해 파일 객체를 사용한다 (메모리에 전체 적재 금지).

실행:
  python poc_06_largefile.py                  # 100MB 왕복
  python poc_06_largefile.py --size-mb 500    # 500MB 왕복
  python poc_06_largefile.py --kill-test      # 업로드 중단 실험 포함
  python poc_06_largefile.py --upload-only    # (내부용) kill-test의 자식 프로세스 모드
(선행: poc_01, poc_02)
"""
import argparse
import os
import subprocess
import sys
import time

from poc_common import PocClient, TMP_DIR, file_digests, require_drive_id

CHUNK = 1024 * 1024


def make_random_file(path, size_mb: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for _ in range(size_mb):
            f.write(os.urandom(CHUNK))
    return path


def streamed_upload(pc: PocClient, drive_id: str, parent_id: str, filepath, filename: str):
    """파일 객체 기반 스트리밍 업로드 (307 수동 처리 — 재시도 시 파일 재오픈)."""
    path = f"/drive/v1/drives/{drive_id}/files?parentId={parent_id}"
    # retry429=False: 파일 객체는 1회 소비되므로 자동 재시도 금지 (재시도는 파일 재오픈으로 호출자가 수행)
    with open(filepath, "rb") as f:
        r1 = pc.request("POST", path, files={"file": (filename, f)}, retry429=False, label="large-first-hop")
    info = {"first_status": r1.status_code}
    if r1.status_code in (301, 302, 303, 307):
        loc = r1.headers.get("location", "")
        info["location_host"] = loc.split("/")[2] if loc.startswith("http") else None
        with open(filepath, "rb") as f:  # multipart 재구성: 파일 재오픈
            r2 = pc.request("POST", loc, files={"file": (filename, f)}, retry429=False, label="large-second-hop")
        info["second_status"] = r2.status_code
        final = r2
    else:
        final = r1
    body = final.json()
    if not (final.status_code == 200 and body.get("header", {}).get("isSuccessful")):
        raise RuntimeError(f"대용량 업로드 실패 HTTP {final.status_code}: {str(body)[:300]}")
    return info, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mb", type=int, default=100)
    ap.add_argument("--kill-test", action="store_true")
    ap.add_argument("--upload-only", action="store_true", help="내부용: 업로드만 수행(중단 실험 대상)")
    args = ap.parse_args()

    name = "06_largefile_child" if args.upload_only else "06_largefile"
    pc = PocClient(name)
    try:
        drive_id = require_drive_id()
        sandbox_id = pc.ensure_sandbox(drive_id)
        local = TMP_DIR / f"poc06_{args.size_mb}mb.bin"

        if args.upload_only:
            # kill-test 자식 모드: 업로드만 시도 (부모가 도중에 kill)
            streamed_upload(pc, drive_id, sandbox_id, local, f"poc06_killtest_{args.size_mb}mb.bin")
            return

        pc.log(f"랜덤 파일 생성 중: {args.size_mb}MB")
        make_random_file(local, args.size_mb)
        digests = file_digests(local.read_bytes()) if args.size_mb <= 200 else None
        remote_name = f"poc06_{args.size_mb}mb.bin"

        # 1) 업로드 시간 측정
        t0 = time.monotonic()
        info, body = streamed_upload(pc, drive_id, sandbox_id, local, remote_name)
        up_sec = time.monotonic() - t0
        file_id = body["result"]["id"]
        pc.results["upload"] = {"info": info, "seconds": round(up_sec, 1), "mb_per_s": round(args.size_mb / up_sec, 2), "file_id": file_id}
        pc.log(f"업로드 완료: {up_sec:.1f}s ({args.size_mb / up_sec:.2f} MB/s)")

        # 2) 메타로 크기 검증
        meta = pc.file_meta(drive_id, file_id)["result"]
        pc.results["size_verified"] = meta.get("size") == local.stat().st_size
        pc.log(f"서버 크기 일치: {pc.results['size_verified']} ({meta.get('size')})")

        # 3) 다운로드 시간 측정 + 무결성
        dest = TMP_DIR / f"poc06_dl_{args.size_mb}mb.bin"
        t0 = time.monotonic()
        dl_info = pc.download_raw(drive_id, file_id, dest)
        dl_sec = time.monotonic() - t0
        pc.results["download"] = {"info": dl_info, "seconds": round(dl_sec, 1), "mb_per_s": round(args.size_mb / dl_sec, 2)}
        pc.log(f"다운로드 완료: {dl_sec:.1f}s ({args.size_mb / dl_sec:.2f} MB/s)")
        if digests:
            ok = file_digests(dest.read_bytes())["sha256_hex"] == digests["sha256_hex"]
            pc.results["integrity_ok"] = ok
            pc.log(f"무결성: {'PASS' if ok else 'FAIL'}")
        else:
            pc.results["integrity_ok"] = dest.stat().st_size == local.stat().st_size
            pc.log(f"크기 대조(200MB 초과는 해시 생략): {pc.results['integrity_ok']}")

        pc.move_to_trash(drive_id, file_id)

        # 4) (옵션) 업로드 중단 실험
        if args.kill_test:
            pc.log("업로드 중단 실험: 자식 프로세스 시작 → 업로드 예상시간의 ~40% 시점에 kill")
            kill_after = max(3, up_sec * 0.4)
            child = subprocess.Popen(
                [sys.executable, __file__, "--upload-only", "--size-mb", str(args.size_mb)],
                cwd=str(local.parent.parent),
            )
            time.sleep(kill_after)
            child.kill()
            child.wait()
            pc.log(f"자식 프로세스 kill 완료 ({kill_after:.0f}s 시점). 10초 후 서버 상태 확인")
            time.sleep(10)
            partial = pc.find_child_by_name(drive_id, sandbox_id, f"poc06_killtest_{args.size_mb}mb.bin")
            pc.results["kill_test"] = {
                "killed_after_sec": round(kill_after, 1),
                "partial_file_on_server": partial,
            }
            pc.log(f"중단 후 서버에 파일 존재: {bool(partial)}"
                   + (f" (size={partial.get('size')}) ← 부분 파일 잔존 여부 확인" if partial else " ← 쓰레기 없음(정상)"))
            if partial:
                pc.move_to_trash(drive_id, partial["id"])
            # 재시도(전체 재전송)
            t0 = time.monotonic()
            _, body2 = streamed_upload(pc, drive_id, sandbox_id, local, f"poc06_retry_{args.size_mb}mb.bin")
            pc.results["retry_after_kill"] = {"ok": True, "seconds": round(time.monotonic() - t0, 1)}
            pc.log("중단 후 전체 재전송 성공")
            pc.move_to_trash(drive_id, body2["result"]["id"])

        # 로컬 정리
        local.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        pc.save()
    finally:
        pc.close()


if __name__ == "__main__":
    main()
