"""프로파일의 sync 정책(sync_mode/sync_note)을 바꾼다 — 정책 변경의 단일 창구.

config.toml은 프로그램이 재작성하는 파일이라 손으로 고치지 않는다. 이 도구가
load_config → 수정 → save_config 왕복으로 안전하게 바꾼다(타 프로파일·미지 키 보존).

사용:  python tools\\set_sync_mode.py --list
       python tools\\set_sync_mode.py <프로파일> <sync|push|pull|off> [--note "사유"]

의미:  sync = 양방향(synchere/SYNC.ps1 -Sync 대상)
       push/pull = 그 방향 수동 명령만 권장, sync 실행은 CLI 게이트가 거부
       off  = 자동 동작 없음(수동 push/pull만)
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
import tomllib
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dooray_sync.config import config_path, load_config, save_config   # noqa: E402

# 마커 규칙(AUTO_OFF_PREFIX, _ensure_local_marker)의 단일 정본은 sync_here.py다.
# tools/는 패키지가 아니라 파일 경로로 로드해 재사용한다(상수·로직 중복 금지).
_spec = importlib.util.spec_from_file_location(
    "_sync_here_for_policy", Path(__file__).with_name("sync_here.py"))
_sync_here = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sync_here)

MODES = ("sync", "push", "pull", "off")


def _list() -> int:
    with open(config_path(), "rb") as f:
        doc = tomllib.load(f)
    print(f"{'프로파일':<12} {'sync_mode':<10} 사유")
    print("-" * 60)
    for name, body in (doc.get("profile") or {}).items():
        mode = str((body or {}).get("sync_mode") or "(미설정)")
        note = str((body or {}).get("sync_note") or "")
        print(f"{name:<12} {mode:<10} {note}")
    return 0


def main() -> int:
    args = list(sys.argv[1:])
    if args == ["--list"] or not args:
        return _list() if args else (print(__doc__) or 2)
    if len(args) < 2:
        print(__doc__)
        return 2
    name, mode = args[0], args[1].strip().lower()
    note = None
    if "--note" in args:
        i = args.index("--note")
        if i + 1 < len(args):
            note = args[i + 1]
    if mode not in MODES:
        print(f"sync_mode 값이 올바르지 않습니다: {mode!r} (sync | push | pull | off)")
        return 2
    try:
        p = load_config(name)
    except FileNotFoundError as exc:
        print(str(exc))
        return 2
    before = (p.sync_mode, p.sync_note)
    p.sync_mode = mode
    if note is not None:
        p.sync_note = note
    elif p.sync_note.startswith(_sync_here.AUTO_OFF_PREFIX):
        # 사람이 이 도구로 모드를 명시하면 자동 해제 태그를 걷어낸다 — 태그가
        # 남으면 마커 파일이 (GDrive 복원 등으로) 되살아나는 순간 자동 재등록이
        # 사람의 결정을 뒤집는다(적대 검증 지적). 태그 없는 값 = 수동 상태.
        p.sync_note = f"{datetime.date.today().isoformat()} 수동 {mode} 설정"
    save_config(p)
    print(f"[{name}] sync_mode: {before[0]!r} → {p.sync_mode!r}")
    if p.sync_note != before[1]:
        print(f"[{name}] sync_note: {before[1]!r} → {p.sync_note!r}")
    if mode == "sync":
        # sync 전환 = 등록. '등록 = 루트에 마커 ON' 불변식(구현계획서 M2.5) —
        # 마커 없이 두면 다음 정합이 방금 켠 sync를 도로 해제한다(적대 검증 지적:
        # CLI 게이트·synchere가 안내하는 공식 전환 경로가 이 함정에 빠졌었다).
        _sync_here._ensure_local_marker(p.local_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
