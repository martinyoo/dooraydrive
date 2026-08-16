"""폴더 안 `synchere.bat`의 본체 — 배치파일이 놓인 폴더를 동기화한다.

배치파일은 자기 위치(%~dp0)를 `--root`로 넘긴다. 위치 해석은 두 방향이다:

  상향 — 이 폴더를 **품는** 프로파일이 있으면(local_root 자신 또는 그 하위)
         그 프로파일 하나를 동기화한다. 최장 접두 일치로 구현했다.
  하향 — 품는 프로파일이 없으면 이 폴더 **아래에 있는** 프로파일 전부를 차례로
         동기화한다(WORK처럼 여러 동기화 폴더를 거느린 상위 폴더용).

둘 다 아니면 **자동 등록**한다(2026-08-10 사용자 요구: 대상 폴더를 미리 정하지
않는다 — synchere.bat 실행이 곧 등록이다). 원격 경로는 추정하지 않고 유도한다:

  1) 형제 유도 — 같은 부모 아래 등록된 형제가 있으면 부모의 로컬↔원격 결합에서
     유도(예: 'WORK/spri 2025'가 등록돼 있으면 그 형제 폴더는 'WORK/…'로 붙는다).
  2) 원격 마커 발견 — 개인 드라이브를 깊이 제한 BFS로 훑어, 마커(synchere.bat)가
     있고 이름이 같은 원격 폴더에 결합(다른 PC에서 이미 동기화하던 폴더를 이어받기).
     동명이 여럿이면 자동 결합하지 않고 목록을 보여 준다(오결합 방지).
  3) 최상위 동명 폴더 — 마커는 없지만 드라이브 최상위에 이름이 같은 폴더가 있으면
     결합(웹에서 만들어 둔 폴더를 처음 내려받는 경우).
  4) 신규 — 드라이브 최상위에 같은 이름의 원격 폴더를 새로 만든다.

프로그램 폴더 안의 원본(템플릿)과 드라이브 루트는 등록을 거부한다. 설정 파일이
아직 없는 새 PC에서도 그대로 동작한다(첫 등록이 설정을 만든다).

sync 실행 여부는 config.toml의 **sync_mode**가 정한다(단일 정본 — 예전의 하드코딩
제외 표는 폐지). 'sync'만 실행하고, 나머지(push/pull/off/미설정)는 사유와 대안을
안내한다. 변경은 `python tools\\set_sync_mode.py`로.

**로컬 마커(프로파일 루트의 synchere.bat)는 등록/해제 스위치다**(2026-08-07 사용자
요구: 복사해 실행하면 등록, 지우면 동기화 대상에서 해제). 정본은 여전히 config다 —
마커는 실행 시점의 판단 근거가 아니라, 관찰될 때 config를 고쳐 쓰는 입력이다.
규칙은 `reconcile_markers` 한 곳에 있다(SYNC.ps1 -Sync도 --check-markers로 호출):

  - sync_mode='sync'인데 루트에 마커 없음 → 자동 해제(off + [synchere-off] 태그).
    프로파일·기준선 DB는 남긴다(soft-delete) — 일시적 부재(GDrive 재동기화·백신
    격리)가 등록 정보를 파괴하지 못하게 하고, 재등록을 더블클릭 한 번으로 만든다.
  - off + [synchere-off] 태그인데 마커 있음(재복사 후 실행) → 자동 재등록(sync 복귀).
  - push/pull/태그 없는 off는 **사람의 결정이므로 절대 자동 전환하지 않는다**
    (더블클릭 한 번이 workenv의 1GB 수신 회피나 writing의 안전 보류를 뒤집으면 안 됨).

성공적으로 동기화한 프로파일의 원격 루트에는 **마커(synchere.bat)를 자동 유지**한다
— 원격 마커는 발견(discovery) 힌트일 뿐이며 sync 엔진에는 보이지 않는다(양축
ALWAYS_EXCLUDE). 해제 시에도 원격 마커는 지우지 않는다(삭제 무전파 + 다른 PC의
발견 힌트 — 정리는 tools/trash_toolfile.py 수동). dry-run에서는 아무것도 쓰지 않는다.

실행:  python tools\\sync_here.py [--root <폴더>] [dsync sync 추가 인자...]
       python tools\\sync_here.py --check-markers [--dry-run]   (정합만, sync 없음)
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import tomllib
from pathlib import Path

# pythonw(콘솔 없음)에서는 sys.stdout/stderr가 None이라 맨 reconfigure가
# AttributeError로 죽는다(2026-08-16 실측) — cli/main.py:30-34와 같은 방어.
# 무인 러너(M3)가 이 모듈의 함수를 임포트해 쓰므로 임포트 시점에 죽으면 안 된다.
for _stream_name in ("stdout", "stderr"):
    try:
        getattr(sys, _stream_name).reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dooray_sync.config import config_path      # noqa: E402
from dooray_sync.util.paths import ext_path, to_nfc   # noqa: E402

MARKER = "synchere.bat"
# 자동 해제를 사람의 결정(off)과 구분하는 태그 — sync_note 맨 앞에 붙는다.
# 재등록 자동 복귀는 이 태그가 있는 off에만 적용된다.
AUTO_OFF_PREFIX = "[synchere-off]"


def _child_env() -> dict[str, str]:
    """자식(python -m dooray_sync...)이 패키지를 찾도록 PYTHONPATH로 주입한다.

    예전에는 cwd=REPO로 해결했는데, Windows에서 어떤 프로세스의 CWD인 폴더는
    rename이 안 된다 — 설치.bat 갱신 모드(fca71ed)의 폴더 통째 교체가 동기화
    도중이면 :refresh_locked로 막힌다(2026-08-16 실측, 설계 I-A8). CWD는 부모
    것(사용자 폴더)을 물려받고 모듈 탐색만 환경변수로 해결한다. 자식 명령의
    -P(sys.path[0] 미주입, Python 3.11+)와 짝이다 — 사용자 데이터 폴더에 우연히
    있는 동명 모듈이 CWD 경유로 끼어드는 것을 막는다.
    """
    env = dict(os.environ)
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO) + (os.pathsep + prev if prev else "")
    return env


def _norm(p: str | Path) -> str:
    """비교용 절대경로 키: 절대화 + NFC + casefold. 끝 구분자 제거."""
    s = os.path.abspath(str(p))
    return to_nfc(s).casefold().rstrip("\\/")


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + "\\") or child.startswith(parent + "/")


def _load_profiles() -> dict[str, dict]:
    """{프로파일명: {root, mode(None=미설정), note}}. 설정이 없으면 빈 dict."""
    cp = config_path()
    if not os.path.exists(cp):
        return {}
    with open(cp, "rb") as f:
        doc = tomllib.load(f)
    out: dict[str, dict] = {}
    for name, body in (doc.get("profile") or {}).items():
        body = body or {}
        root = str(body.get("local_root") or "").strip()
        if not root:
            continue
        # 키 없음 = 기본 'sync' — load_config의 기본값과 정렬(2026-08-07 사용자 피드백:
        # sync_mode 도입 전에 만들어진 config에서 전부 '미설정 제외'가 떠 혼란).
        # 정책은 PC별 config 소관이므로 기본 sync가 각 PC에서 올바른 해석이다.
        mode = str(body.get("sync_mode") or "sync").strip().lower()
        out[name] = {
            "root": root,
            "mode": mode,
            "note": str(body.get("sync_note") or "").strip(),
            "remote": str(body.get("remote_path") or "").strip(),
            "drive_id": str(body.get("drive_id") or "").strip(),
            "base_url": str(body.get("base_url") or "").strip(),
        }
    return out


def _marker_state(root: str) -> bool | None:
    """루트의 로컬 마커 상태: True(있음) / False(없음) / None(판단 불가).

    os.path.exists는 권한 거부·잠금(백신 스캔 순간)도 False로 뭉갠다 — config.py
    _read_doc이 실측으로 배운 것과 같은 함정이라, 판정 불가를 '삭제 의도'로
    기록하지 않도록 3상으로 구분한다. 루트 폴더 자체가 없으면 False다(존재하지
    않는 폴더를 동기화 대상으로 계속 두는 쪽이 더 위험 — 회귀 테스트 근거)."""
    try:
        os.stat(ext_path(os.path.join(os.path.abspath(str(root)), MARKER)))
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def _ensure_local_marker(root: str) -> None:
    """등록 직후 루트에 로컬 마커를 놓는다 — '등록 = 마커 ON'이 불변식이라,
    --root 직접 호출처럼 마커 없이 등록된 경우 다음 정합이 도로 해제해 버린다.
    실패해도 경고만(등록·동기화 결과에 영향 금지)."""
    try:
        dst = ext_path(os.path.join(os.path.abspath(str(root)), MARKER))
        if not os.path.exists(dst):
            import shutil
            shutil.copyfile(ext_path(REPO / MARKER), dst)
            print(f"       (로컬 마커 배치: {os.path.join(str(root), MARKER)})")
    except OSError as exc:
        print(f"       (로컬 마커 배치 실패 — 무시함: {type(exc).__name__}: {exc})")


def _set_mode(name: str, mode: str, note: str) -> bool:
    """config에 sync_mode/sync_note 기록 — set_sync_mode.py와 같은 왕복(단일 작성자).
    실패는 삼키고 보고만 한다(호출측이 fail-closed 처리)."""
    try:
        from dooray_sync.config import load_config, save_config
        p = load_config(name)
        p.sync_mode = mode
        p.sync_note = note
        save_config(p)
        return True
    except Exception as exc:  # noqa: BLE001 — 정합은 sync 본체를 죽이면 안 된다
        print(f"       (config 기록 실패 — {type(exc).__name__}: {exc})")
        return False


def reconcile_markers(
    profiles: dict[str, dict], *, dry_run: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    """로컬 마커 ↔ config 정합. 마커 = 등록/해제 스위치(모듈 docstring의 규칙).

    profiles의 mode/note를 제자리 갱신하고 (변경 목록[(이름, 새 mode)], 기록 실패
    이름 목록)을 돌려준다. dry_run이면 config를 쓰지 않는다 — 해제는 미리보기의
    일관성을 위해 메모리에서만 제외하고, 재등록은 하지 않는다(기록 없이 sync를
    돌리면 CLI 게이트가 off를 보고 거부하므로 예고만 한다).
    """
    today = datetime.date.today().isoformat()
    changed: list[tuple[str, str]] = []
    failed: list[str] = []
    for name, info in profiles.items():
        state = _marker_state(info["root"])
        if state is None:
            # 판단 불가(권한·잠금)는 해제 근거가 아니다 — config를 바꾸지 않는다.
            print(f"[보류] '{name}' — 마커 상태를 확인할 수 없습니다(권한·잠금). "
                  f"config를 바꾸지 않습니다: {info['root']}")
            continue
        if info["mode"] == "sync" and not state:
            # 주의: 이 note는 SYNC.ps1이 cp949 파이프로 되읽는다 — em dash(—) 등
            # cp949 비인코딩 문자를 넣지 말 것(전역 CLAUDE.md 인코딩 교훈).
            note = (f"{AUTO_OFF_PREFIX} {today} 로컬 {MARKER} 없음 - "
                    f"재등록: 폴더에 {MARKER} 복사 후 실행")
            if dry_run:
                print(f"[해제 예정] '{name}' — 루트에 {MARKER} 없음 "
                      f"(dry-run: 기록 안 함, 이번 미리보기에서도 제외)")
                print(f"       {info['root']}")
                changed.append((name, "off"))
            else:
                print(f"[해제] '{name}' — 루트에 {MARKER} 없음")
                print(f"       {info['root']}")
                if _set_mode(name, "off", note):
                    print(f"       sync_mode=off 기록됨. 재등록: 위 폴더에 {MARKER} "
                          f"복사 후 실행 (기준선 DB는 보존됨)")
                    print(f"       영구 해제로 굳히려면: python tools\\set_sync_mode.py "
                          f"{name} off  (마커가 되살아나도 재등록 안 됨)")
                    changed.append((name, "off"))
                else:
                    print("       기록 실패 — 이번 실행에서는 제외하지만 config에는 "
                          "아직 sync로 남아 있습니다. 다시 실행하면 재시도됩니다.")
                    failed.append(name)
            # 기록 실패·dry-run이어도 이번 실행에서는 제외한다(fail-closed).
            info["mode"], info["note"] = "off", note
            info["_reconcile_reported"] = True
        elif (info["mode"] == "off" and state
              and info["note"].startswith(AUTO_OFF_PREFIX)):
            if dry_run:
                print(f"[재등록 예정] '{name}' — {MARKER} 재확인 (dry-run: 기록 안 함. "
                      f"--dry-run 없이 실행하면 sync가 재개됩니다)")
                info["_pending_reenable"] = True
                info["_reconcile_reported"] = True
                continue
            if _set_mode(name, "sync", f"{today} {MARKER} 재확인으로 자동 재등록"):
                print(f"[재등록] '{name}' — {MARKER} 재확인 → sync_mode=sync")
                info["mode"], info["note"] = "sync", ""
                changed.append((name, "sync"))
            else:
                print(f"[재등록 실패] '{name}' — config 기록 불가, off 유지")
                info["_reconcile_reported"] = True
                failed.append(name)
        # 그 외(push/pull/태그 없는 off, sync+마커 있음)는 사람의 결정 — 손대지 않음
    return changed, failed


# 원격에서 동명 폴더를 찾는 최대 깊이. 실제 동기화 루트는 깊이 1~2다
# ('WORK/SW통계'=2, '근무환경'=1). 3으로 올리면 안 된다 — 아래 비용 구조 때문이다.
#
# 이름은 부모를 조회해야 보이므로, 깊이 N까지 찾으려면 깊이 0~N-1의 폴더를 전부
# 조회해야 한다. 실측(2026-08-10, 이 계정): 깊이 0=1건, 깊이 1=6건(~6초),
# 깊이 2=124건(~60~120초). 즉 N=2는 ~7회 호출로 끝나지만 N=3은 분 단위가 되어
# 더블클릭 한 번의 대기로는 쓸 수 없다(실측: 5분 초과로 중단).
#
# 마커 확인은 **이름이 일치하는 후보만** 추가 조회한다. 예전 구현은 순회하는 모든
# 폴더의 마커를 확인해 조회 수가 폴더 수와 같아졌다 — 그것이 위 폭발의 원인이었다.
DISCOVER_DEPTH = 2


def _new_profile_name(leaf: str, profiles: dict[str, dict]) -> str:
    """폴더 이름에서 프로파일명 생성. config의 _PROFILE_NAME_RE(`[A-Za-z0-9._-]+`)를
    반드시 만족해야 한다 — 어기면 init이 ValueError로 죽는다.

    **str.isalnum()을 쓰면 안 된다**: 한글도 True라 'SW통계'가 그대로 통과해
    등록이 실패했다(2026-08-10 실측). 한국어 폴더명이 기본인 환경이라 이 경로가
    사실상 항상 걸린다. ASCII만 남기고, 남는 게 없으면 'folder'로 떨어진다.
    """
    pname = "".join(
        c for c in leaf
        if (c.isascii() and c.isalnum()) or c in "._-"
    ).strip("._-")
    if not pname or pname in (".", ".."):
        pname = "folder"
    # config_exists도 본다 — _load_profiles는 local_root 없는 항목을 걸러내므로
    # 그런 항목과 이름이 겹치면 init이 '이미 있습니다'로 거부한다.
    from dooray_sync.config import config_exists
    base_name, n = pname, 1
    while pname in profiles or config_exists(pname):
        n += 1
        pname = f"{base_name}{n}"
    return pname


def _derive_from_sibling(abs_root: str, profiles: dict[str, dict]) -> tuple | None:
    """형제 유도: 같은 부모 아래 등록된 형제가 있으면 (drive_id, 원격경로, create=True).

    부모의 로컬↔원격 결합이 이미 확정돼 있으므로 이 폴더의 원격 경로는 추정이
    아니라 유도다. 없으면 None(다음 단계인 드라이브 유도로 넘어간다).
    """
    parent_key = _norm(os.path.dirname(abs_root))
    leaf = to_nfc(os.path.basename(abs_root))
    for _n, info in profiles.items():
        if info["remote"] and _norm(os.path.dirname(info["root"])) == parent_key:
            remote_parent = info["remote"].replace("\\", "/").rstrip("/").rpartition("/")[0]
            candidate = f"{remote_parent}/{leaf}" if remote_parent else leaf
            return (info["drive_id"], candidate, True)
    return None


def _derive_from_drive(leaf: str, profiles: dict[str, dict]) -> tuple[tuple | None, str, list[str]]:
    """드라이브 유도: (binding, how, why). binding=(drive_id, 원격경로, create여부).

    binding이 None이면 why에 사용자 안내 줄들이 온다(호출측이 출력 후 2로 종료).
    토큰 없음·통신 오류는 예외로 던진다 — 호출측이 구분해 처리한다.
    """
    from dooray_sync.api.client import DoorayClient
    from dooray_sync.api.drive import DriveAPI
    from dooray_sync.auth import get_token
    from dooray_sync.config import Profile
    from dooray_sync.util.paths import path_key

    base_url = next(
        (p["base_url"] for p in profiles.values() if p.get("base_url")),
        Profile().base_url)
    want = path_key(leaf)

    with DoorayClient(base_url, get_token()) as client:
        api = DriveAPI(client)
        drives = api.list_drives()  # type=private — 개인 드라이브만 자동 결합 대상
        if not drives:
            return None, "", [
                "접근 가능한 개인 드라이브가 없습니다. 토큰 권한과 IP ACL 설정을 확인하세요."]
        if len(drives) > 1:
            return None, "", [
                "개인 드라이브가 여러 개라 자동 결합하지 않습니다. 직접 지정하세요:",
                '  dsync init -p <이름> --drive-id <드라이브id> '
                '--local-root "<로컬>" --remote-path "<원격/경로>"']
        drive_id = str(drives[0].get("id") or "")
        root_id = api.find_root_folder(drive_id)

        # 1단계: 이름 탐색. 깊이 0~DISCOVER_DEPTH-1의 폴더만 조회한다(자식 이름은
        # 부모 조회로 얻는다). 여기서 마커를 확인하지 않는 것이 핵심이다 — 확인하면
        # 조회 수가 폴더 수와 같아져 분 단위가 된다(상수 주석의 실측).
        print("원격에서 같은 이름의 폴더를 찾는 중입니다...")
        cand: list[tuple[str, str]] = []   # 이름이 일치하는 (folder_id, 원격경로)
        root_names: dict[str, str] = {}    # path_key → 실제 표기 (최상위 폴더)
        queue: list[tuple[str, str, int]] = [(root_id, "", 0)]
        while queue:
            fid, path, d = queue.pop(0)
            for c in api.iter_children(drive_id, fid):
                if not c.is_dir:
                    continue
                cname = to_nfc(c.name)
                cpath = f"{path}/{cname}" if path else cname
                if d == 0:
                    root_names.setdefault(path_key(cname), cname)
                if path_key(cname) == want:
                    cand.append((c.id, cpath))
                if d + 1 < DISCOVER_DEPTH:
                    queue.append((c.id, cpath, d + 1))

        # 2단계: 후보에서만 마커를 확인한다(보통 0~2건).
        matches = [
            cpath for cid, cpath in cand
            if api.find_child_by_name(drive_id, cid, MARKER) is not None
        ]

    if len(matches) == 1:
        return (drive_id, matches[0], False), "원격 마커 발견 — 기존 동기화 폴더를 이어받습니다", []
    if len(matches) > 1:
        return None, "", (
            ["같은 이름의 동기화 폴더(마커)가 원격에 여러 개 있어 자동 결합하지 않습니다:"]
            + [f"  {m}" for m in matches]
            + ["원격 경로를 직접 지정하세요:",
               f'  dsync init -p <이름> --drive-id {drive_id} '
               '--local-root "<로컬>" --remote-path "<원격/경로>"'])
    hit = root_names.get(want)
    if hit is not None:
        return (drive_id, hit, False), "드라이브 최상위의 같은 이름 폴더에 결합합니다", []
    return (drive_id, leaf, True), "원격에 같은 이름이 없어 드라이브 최상위에 새로 만듭니다", []


def _register_and_sync(abs_root: str, profiles: dict[str, dict],
                       drive_id: str, remote: str, create: bool,
                       how: str, extra: list[str]) -> int:
    """유도된 결합으로 등록(dsync init)하고 이어서 동기화한다. 공통 마무리 경로."""
    leaf = to_nfc(os.path.basename(abs_root))
    pname = _new_profile_name(leaf, profiles)
    print("미등록 폴더입니다 — 동기화 대상으로 등록합니다:")
    print(f"  로컬  {abs_root}")
    print(f"  원격  {remote}   (프로파일 '{pname}')")
    print(f"  근거  {how}")
    print()
    if "--dry-run" in extra:
        print("dry-run — 등록·동기화 없이 계획만 보였습니다. 실행하면 위대로 등록 후 동기화합니다.")
        return 0
    cmd = [sys.executable, "-P", "-m", "dooray_sync.cli.main", "init", "-p", pname,
           "--drive-id", drive_id, "--local-root", abs_root, "--remote-path", remote]
    if create:
        cmd.append("--create-remote")
    rc = subprocess.call(cmd, env=_child_env())
    if rc != 0:
        print(f"등록 실패(종료코드 {rc}) — 직접 확인이 필요합니다.")
        # 자식의 원시 종료코드를 그대로 흘리면 배치(synchere.bat:72-83)의 재시도
        # 규약(1=키 입력 재시도, 2=안내 후 종료)이 깨진다 — 2만 보존, 나머지는 1.
        return 2 if rc == 2 else 1
    _ensure_local_marker(abs_root)
    print()
    return _run_sync(pname, abs_root, extra)


def _explain_skip(name: str, info: dict) -> None:
    mode, note = info["mode"], info["note"]
    if mode == "off" and note.startswith(AUTO_OFF_PREFIX):
        print(f"[제외] 프로파일 '{name}' — {MARKER} 삭제로 자동 해제됨 ({note})")
        print(f"       재등록: {info['root']} 에 {MARKER} 복사 후 실행")
        print(f"       영구 해제로 굳히려면: python tools\\set_sync_mode.py {name} off")
        return
    print(f"[제외] 프로파일 '{name}' — sync_mode={mode}"
          + (f" ({note})" if note else ""))
    if mode in ("push", "pull"):
        print(f"       수동 실행: dsync {mode} -p {name}")
    else:
        print(f"       수동 운용: dsync push -p {name} / dsync pull -p {name}")
    print(f"       전환: python tools\\set_sync_mode.py {name} sync")


def _ensure_marker(name: str) -> None:
    """성공한 sync 뒤 원격 루트에 마커가 없으면 올린다 — 실패해도 경고만."""
    try:
        from dooray_sync.api.client import DoorayClient
        from dooray_sync.api.drive import DriveAPI
        from dooray_sync.auth import get_token
        from dooray_sync.config import load_config
        from dooray_sync.core.remote import resolve_remote_root

        p = load_config(name)
        with DoorayClient(p.base_url, get_token()) as client:
            api = DriveAPI(client)
            root_id, _ = resolve_remote_root(api, p.drive_id, p.remote_path)
            if api.find_child_by_name(p.drive_id, root_id, MARKER) is None:
                api.upload_new(p.drive_id, root_id, MARKER, REPO / MARKER)
                print(f"       (원격 마커 복구: {p.remote_path}/{MARKER})")
    except Exception as exc:  # noqa: BLE001 — 마커는 힌트일 뿐, sync 결과에 영향 금지
        print(f"       (원격 마커 확인 실패 — 무시함: {type(exc).__name__}: {exc})")


def _run_sync(name: str, root: str, extra: list[str]) -> int:
    print(f"[프로파일 '{name}'] {root}")
    cmd = [sys.executable, "-P", "-m", "dooray_sync.cli.main", "sync", "-p", name, *extra]
    rc = subprocess.call(cmd, env=_child_env())
    if rc == 0 and "--dry-run" not in extra:
        _ensure_marker(name)
    return rc


def resolve_targets(root: str | Path, profiles: dict[str, dict]) -> list[tuple[str, dict]]:
    """--root가 가리키는 실행 대상 집합(모듈 docstring의 상향/하향 규칙).

    상향(품는 프로파일)이 있으면 최장 접두 일치 하나, 없으면 이 폴더 아래의
    프로파일 전부. 하향 집합은 **이름순으로 정렬**한다 — dict 순서(=config 파일
    순서)에 실행 순서를 묶으면 문서상 마지막 프로파일이 상습적으로 늦고, M3
    러너와 사람 경로가 같은 입력에서 다른 순서로 돌게 된다(설계 §2.6).
    """
    base = _norm(root)
    containing: tuple[str, dict] | None = None
    for name, info in profiles.items():
        lkey = _norm(info["root"])
        if _under(base, lkey):
            if containing is None or len(lkey) > len(_norm(containing[1]["root"])):
                containing = (name, info)
    if containing is not None:
        return [containing]
    down = [(n, i) for n, i in profiles.items() if _under(_norm(i["root"]), base)]
    return sorted(down, key=lambda t: t[0].casefold())


def main(argv: list[str]) -> int:
    args = list(argv)
    root = os.getcwd()
    if "--root" in args:
        i = args.index("--root")
        if i + 1 >= len(args):
            print("사용법: sync_here.py [--root <폴더>] [추가 인자...]")
            return 2
        root = args[i + 1]
        del args[i:i + 2]
    extra = args

    # --auto <동사>는 이 파일(단일 접점)이 소비한다 — 자식(dsync sync)까지 흘러가면
    # typer가 알 수 없는 인자로 죽고, 그 원시 종료코드가 배치의 30회 재시도 루프를
    # 깨울 수 있다(설계 §2.5).
    # 종료코드 계약(§2.4): --auto 경로는 0 또는 2만 — 1(재시도 유도)을 절대 내지 않는다.
    auto_verb: str | None = None
    auto_all = False
    if "--auto" in extra:
        i = extra.index("--auto")
        if i + 1 >= len(extra):
            print("사용법: synchere.bat --auto <on|off|status|now|loop> [--all]")
            return 2
        auto_verb = extra[i + 1]
        del extra[i:i + 2]
        if "--all" in extra:
            auto_all = True
            extra.remove("--all")

    # 설정이 아직 없어도 계속 간다 — 새 PC의 첫 등록이 설정을 만든다(자동 등록 경로).
    profiles = _load_profiles()

    if auto_verb is not None:
        from dooray_sync.auto.cli import dispatch
        # 대상 해석은 사람 경로와 **같은 함수**를 쓴다 — 상향/하향 규칙이 갈라지면
        # "이 폴더에서 켰는데 다른 게 켜졌다"가 된다.
        targets = resolve_targets(root, profiles)
        try:
            return dispatch(auto_verb, profiles=profiles, targets=targets,
                            all_=auto_all, extra=extra)
        except KeyboardInterrupt:
            print()
            print("중단했습니다.")
            return 0

    # 정합 전용 모드 — SYNC.ps1 -Sync가 실행 전에 호출한다(규칙 구현은 이 파일 한 곳).
    # --emit-modes <파일>: 정합 후의 유효 mode를 "이름\t모드\t재등록예정(0|1)" 줄로
    # UTF-8 기록 — dry-run은 config를 안 쓰므로, SYNC.ps1 -DryRun이 미리보기를
    # 실제 실행과 같은 대상 집합으로 맞추는 유일한 통로다(콘솔 캡처는 cp949 문제).
    if "--check-markers" in extra:
        extra.remove("--check-markers")
        emit = None
        if "--emit-modes" in extra:
            i = extra.index("--emit-modes")
            if i + 1 >= len(extra):
                print("사용법: sync_here.py --check-markers [--dry-run] [--emit-modes <파일>]")
                return 2
            emit = extra[i + 1]
            del extra[i:i + 2]
        _changed, failed = reconcile_markers(profiles, dry_run="--dry-run" in extra)
        if emit:
            with open(emit, "w", encoding="utf-8", newline="\n") as f:
                for n, info in profiles.items():
                    pend = "1" if info.get("_pending_reenable") else "0"
                    f.write(f"{n}\t{info['mode']}\t{pend}\n")
        # 기록 실패 = config와 실제 마커 상태가 어긋난 채 남음 → 호출측이 중단하도록 1.
        return 1 if failed else 0

    targets = resolve_targets(root, profiles)

    if not targets:
        # 미등록 폴더 — 자동 등록한다(모듈 docstring의 유도 사슬 1→4).
        # 2026-08-10 사용자 요구: 대상 폴더를 설치 때 미리 정하지 않는다.
        # synchere.bat를 폴더에 복사해 실행하는 것이 곧 등록이다.
        abs_root = os.path.abspath(str(root))
        leaf = to_nfc(os.path.basename(abs_root))

        # 등록 금지 두 곳 — 프로그램 폴더 안의 원본(템플릿)과 드라이브 루트.
        # 원본을 그 자리에서 더블클릭하면 프로그램 폴더 전체가 원격에 올라간다.
        if _under(_norm(abs_root), _norm(REPO)):
            print("이 synchere.bat 는 프로그램 폴더 안의 원본입니다 — 여기는 동기화 대상이 아닙니다.")
            print("동기화할 폴더에 이 파일을 복사한 뒤, 복사본을 더블클릭하세요.")
            return 2
        if not leaf or os.path.dirname(abs_root) == abs_root:
            print(f"드라이브 최상위({abs_root})는 동기화 대상으로 등록할 수 없습니다.")
            print("하위 폴더에 synchere.bat 를 복사해 실행하세요.")
            return 2

        binding = _derive_from_sibling(abs_root, profiles)
        how = "형제 프로파일의 결합에서 유도"
        if binding is None:
            try:
                binding, how, why = _derive_from_drive(leaf, profiles)
            except Exception as exc:  # noqa: BLE001 — 토큰 없음·통신 오류 모두 여기로
                from dooray_sync.auth import TokenNotFound
                if isinstance(exc, TokenNotFound):
                    print("API 토큰이 등록되어 있지 않습니다. 먼저 설치(설치.bat)를 마쳐 주세요.")
                    print(str(exc))
                    return 2
                print(f"원격 조회 실패 — {type(exc).__name__}: {exc}")
                print("네트워크 문제일 수 있습니다. 잠시 후 다시 실행해 보세요.")
                return 1
            if binding is None:
                print(f"이 폴더를 자동 등록하지 못했습니다: {abs_root}")
                for line in why:
                    print(line)
                return 2
        drive_id, remote, create = binding
        return _register_and_sync(abs_root, profiles, drive_id, remote, create, how, extra)

    # 실행 전 마커↔config 정합 — 마커가 지워진 대상은 여기서 해제되어 빠지고,
    # 마커를 되살린(태그 off) 대상은 sync로 복귀해 아래 runnable에 잡힌다.
    _changed, marker_failed = reconcile_markers(dict(targets), dry_run="--dry-run" in extra)

    runnable = [(n, i) for n, i in targets if i["mode"] == "sync"]
    for name, info in targets:
        # 정합이 방금 보고한 프로파일은 다시 설명하지 않는다 — dry-run에서
        # '해제 예정' 직후 과거형 '해제됨'이 찍히는 자기모순 방지(적대 검증 지적).
        if info["mode"] != "sync" and not info.get("_reconcile_reported"):
            _explain_skip(name, info)

    if not runnable:
        # 기록 실패는 여기서도 1로 끝낸다 — 2(안내성 오류)로 끝내면 배치의
        # ANY-KEY 재시도 루프가 뜨지 않아, 일시적 config 잠금이라는 재시도가
        # 정확히 유효한 상황에서 재시도가 차단된다(2차 적대 검증 지적).
        if marker_failed:
            print(f"마커 정합 기록 실패: {', '.join(marker_failed)} — config가 잠겨 "
                  f"있었을 수 있습니다. 다시 실행하면 재시도됩니다.")
            return 1
        # dry-run에서 재등록 예정뿐인 경우: 실제 실행은 sync가 재개되므로
        # 안내성 오류(2)가 아니라 0으로 끝낸다(dry-run과 실제의 종료 의미 정렬).
        if any(i.get("_pending_reenable") for _, i in targets):
            return 0
        return 2

    if len(runnable) > 1:
        print(f"이 폴더 아래 동기화 대상 {len(runnable)}개를 차례로 동기화합니다: "
              + ", ".join(n for n, _ in runnable))
        print()

    failed: list[str] = []
    for name, info in runnable:
        rc = _run_sync(name, info["root"], extra)
        if rc != 0:
            failed.append(name)
        print()

    if failed:
        print(f"실패한 프로파일: {', '.join(failed)} — 다시 실행하면 실패분만 재시도됩니다.")
        return 1
    if marker_failed:
        # 화면·메모리는 '해제'인데 config는 sync로 남은 상태 — 성공(0)으로 닫으면
        # 어긋남이 조용히 굳는다. rc=1이면 배치의 재시도 루프가 일시적 잠금에 대응.
        print(f"마커 정합 기록 실패: {', '.join(marker_failed)} — config가 잠겨 "
              f"있었을 수 있습니다. 다시 실행하면 재시도됩니다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
