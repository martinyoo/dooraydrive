"""설정 경로 진단 — '파일이 있는데 없다고 한다' 문제의 원인을 좁힌다.

같은 경로를 두 프로세스가 다르게 보는 경우는 보통 실행 계정·권한 상승·프로필
리다이렉션 때문이다. 이 스크립트는 그 판별에 필요한 사실만 출력한다.

실행:  python diag.py
"""
from __future__ import annotations

import ctypes
import getpass
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def line(k: str, v) -> None:
    print(f"  {k:<22}: {v}")


print("=" * 68)
print("  Dooray Sync 설정 경로 진단")
print("=" * 68)

print("\n[실행 주체]")
line("파이썬", sys.executable)
line("파이썬 버전", sys.version.split()[0])
line("사용자(getpass)", getpass.getuser())
line("USERNAME", os.environ.get("USERNAME"))
line("USERDOMAIN", os.environ.get("USERDOMAIN"))
line("USERPROFILE", os.environ.get("USERPROFILE"))
try:
    elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
except Exception as exc:  # noqa: BLE001
    elevated = f"확인 실패: {exc}"
line("관리자 권한", elevated)

print("\n[경로]")
appdata = os.environ.get("APPDATA") or ""
line("APPDATA", appdata or "(미설정)")
line("DOORAY_SYNC_CONFIG_DIR", os.environ.get("DOORAY_SYNC_CONFIG_DIR") or "(미설정)")

target = os.path.join(appdata, "dooray-sync") if appdata else ""
cfg = os.path.join(target, "config.toml") if target else ""
line("기대 폴더", target or "(계산 불가)")
line("폴더 존재", os.path.isdir(target) if target else "-")
line("config.toml 존재", os.path.isfile(cfg) if cfg else "-")
if cfg and os.path.isfile(cfg):
    line("config.toml 크기", f"{os.path.getsize(cfg)} bytes")

print("\n[APPDATA 실제 내용 — 'd'로 시작하는 항목]")
try:
    names = sorted(n for n in os.listdir(appdata) if n.lower().startswith("d"))
    print("  " + (", ".join(names) if names else "(없음)"))
    line("APPDATA 총 항목 수", len(os.listdir(appdata)))
except OSError as exc:
    print(f"  열기 실패: {type(exc).__name__}: {exc}")

print("\n[실경로 해석 — 리다이렉션/정션 확인]")
for label, p in (("APPDATA", appdata), ("USERPROFILE", os.environ.get("USERPROFILE") or "")):
    if not p:
        continue
    try:
        line(f"{label} realpath", os.path.realpath(p))
    except OSError as exc:
        line(f"{label} realpath", f"실패 {exc}")

print("\n[CLI가 계산하는 경로]")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dooray_sync.config import config_path, config_exists
    from dooray_sync.util.paths import ext_path

    cp = config_path()
    line("config_path()", cp)
    line("ext_path()", ext_path(cp))
    line("exists(ext)", os.path.exists(ext_path(cp)))
    line("config_exists('spri2025')", config_exists("spri2025"))
except Exception as exc:  # noqa: BLE001
    line("CLI 경로 계산", f"실패 {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 프로파일 인자를 주면 DB 기준선과 로컬 스캔을 대조한다:  python diag.py swstat
# ---------------------------------------------------------------------------
if len(sys.argv) > 1:
    prof = sys.argv[1]
    print(f"\n[프로파일 '{prof}' 기준선 대조]")
    try:
        from dooray_sync.config import db_path, load_config
        from dooray_sync.core.scanner import LocalScanner
        from dooray_sync.store.db import Store

        p = load_config(prof)
        line("drive_id", p.drive_id)
        line("remote_path", p.remote_path or "(드라이브 전체)")
        line("local_root", p.local_root)
        line("DB 경로", db_path(prof))
        line("DB 존재", os.path.isfile(str(db_path(prof))))

        with Store(db_path(prof)) as store:
            base = store.all_by_key(p.drive_id)
            counts = store.count_by_status(p.drive_id)
        line("base 레코드 수", len(base))
        line("sync_status 분포", counts)
        line("local_md5 보유", sum(1 for r in base.values() if r.local_md5))
        line("file_id 보유", sum(1 for r in base.values() if r.file_id))

        scanner = LocalScanner(p.root_path, p.exclude)
        entries = scanner.scan()
        files = {k: e for k, e in entries.items() if not e.is_dir}
        line("로컬 항목(파일만)", len(files))

        common = set(base) & set(files)
        line("키 교집합", len(common))
        targets = [k for k in common
                   if base[k].file_id and not base[k].is_dir and not base[k].local_md5]
        line("reconcile 대상", len(targets))

        if base:
            k = next(iter(base))
            print(f"    base 예시   : key={k!r}")
            r = base[k]
            print(f"                  file_id={r.file_id!r} md5={r.local_md5!r} status={r.sync_status!r}")
        if files:
            k = next(iter(files))
            print(f"    로컬 예시   : key={k!r}")
        only_base = sorted(set(base) - set(files))[:3]
        only_local = sorted(set(files) - set(base))[:3]
        if only_base:
            print(f"    원격에만    : {only_base}")
        if only_local:
            print(f"    로컬에만    : {only_local}")
    except Exception as exc:  # noqa: BLE001
        print(f"    실패: {type(exc).__name__}: {exc}")

print("\n" + "=" * 68)
print("  위 내용을 그대로 복사해 알려 주세요.")
print("=" * 68)
