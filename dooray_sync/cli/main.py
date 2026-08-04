"""dsync CLI 진입점 — 규약 §11.

    dsync init      토큰 확인 → 드라이브 선택 → 로컬 루트 지정 → 설정/DB 생성
                    → 원격 전체 순회로 files 테이블 구축 → changes 커서를 live tip으로 전진
    dsync status    설정/DB 요약, sync_status별 건수, 커서, 마지막 동기화 시각
    dsync push      로컬 → 원격 단방향. **원격의 어떤 것도 삭제·이동하지 않는다.**
    dsync pull      원격 → 로컬 단방향. **수정된 로컬 파일은 절대 덮어쓰지 않는다.**
    dsync doctor    토큰/연결/rate-limit/긴 경로/DB 무결성 점검

M1 범위이므로 삭제 전파는 어느 방향으로도 하지 않는다(구현계획서 §5 M1).
로컬에서 사라진 항목·원격에서 사라진 항목은 **보고만** 한다.

종료코드: 0 성공 / 1 실패 / 2 설정·토큰 문제 / 3 잠금 실패.
"""
from __future__ import annotations

import dataclasses
import os
import sqlite3
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# 규약 §0-1: 진입점에서 즉시 UTF-8 재설정. import 부수효과지만 여기가 유일한 진입점이고,
# 아래 import들이 한국어 메시지를 출력할 수 있으므로 가장 먼저 해야 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass  # 파이프·리다이렉트 등 reconfigure 불가 스트림 — 기본 인코딩으로 진행

import typer

from .. import __version__
from ..api.client import DoorayApiError, DoorayClient
from ..api.drive import DriveAPI
from ..api.models import RemoteFile
from ..auth import TokenNotFound, get_token, mask
from ..config import (
    DEFAULT_EXCLUDE,
    Profile,
    config_exists,
    config_path,
    db_path,
    load_config,
    lock_path,
    save_config,
    state_dir,
)
from ..core.differ import DiffStats, diff
from ..core.executor import SyncExecutor
from ..core.journal import SyncJournal, recover
from ..core.planner import ACTION_LABEL, BulkDeleteAbort
from ..core.planner import plan as build_plan
from ..core.remote import (
    RemoteCollector,
    RemoteRootError,
    iter_known_by_file_id,
    rel_from_remote,
    resolve_remote_root,
)
from ..core.scanner import LocalScanner
from ..logging_setup import current_log_path, setup_logging
from ..store.db import META_LAST_FULL_SCAN, FileRecord, Store, now_iso
from ..util.hashing import md5_file
from ..util.lock import AlreadyRunning, SingleInstanceLock
from ..util.trash import unavailable_reason as trash_unavailable_reason
from ..util.paths import (
    ext_path,
    local_path,
    name_issue,
    path_key,
    rel_posix,
    server_name_will_differ,
    to_nfc,
)
from ..util.trash import send_to_trash

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_LOCK = 3

# status가 읽는 meta 키. db.py의 META_LAST_FULL_SCAN(init/전체 재조정)과 별개로
# 명령별 마지막 수행 시각을 남긴다.
META_LAST_PUSH_AT = "last_push_at"
META_LAST_PULL_AT = "last_pull_at"

# dry-run 표에 한 번에 찍는 최대 행 수. 수만 건짜리 계획을 콘솔에 다 쏟지 않는다.
_MAX_PLAN_ROWS = 200

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Dooray Drive 로컬 동기화 CLI (M1: 수동 단방향 push/pull)",
)


# ---------------------------------------------------------------------------
# 출력 유틸
# ---------------------------------------------------------------------------


def _out(msg: str = "") -> None:
    # flush 고정: stdout은 리다이렉트되면 블록 버퍼링이라 stderr(무버퍼) 경고와
    # 순서가 뒤집힌다. 어떤 경고가 어느 단계에서 났는지가 진단의 핵심이라 순서를 지킨다.
    print(msg, flush=True)


def _err(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


def _dw(s: object) -> int:
    """한글·전각 문자를 2칸으로 계산한 표시 너비. 표 정렬용."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def _pad(s: object, width: int) -> str:
    return str(s) + " " * max(0, width - _dw(s))


def _kv(rows: list[tuple[str, object]], indent: str = "  ") -> None:
    if not rows:
        return
    w = max(_dw(k) for k, _ in rows)
    for k, v in rows:
        _out(f"{indent}{_pad(k, w)} : {v}")


def _table(headers: list[str], rows: list[list[object]], indent: str = "  ") -> None:
    if not rows:
        return
    widths = [_dw(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], _dw(cell))
    _out(indent + "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)))
    _out(indent + "  ".join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        _out(indent + "  ".join(_pad(c, widths[i]) for i, c in enumerate(r)))


def _human_size(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _section(title: str) -> None:
    _out("")
    _out(f"== {title}")


class _Progress:
    """긴 순회의 진행 표시. tty면 한 줄을 갱신하고, 아니면 일정 간격으로 줄을 남긴다."""

    def __init__(self, label: str, every: int = 100) -> None:
        self.label = label
        self.every = max(1, every)
        self.n = 0
        self._tty = bool(getattr(sys.stdout, "isatty", lambda: False)())

    def tick(self, n: int = 1) -> None:
        self.n += n
        if self.n % self.every:
            return
        if self._tty:
            print(f"\r  {self.label}: {self.n}건", end="", flush=True)
        else:
            _out(f"  {self.label}: {self.n}건")

    def done(self, suffix: str = "") -> None:
        line = f"  {self.label}: {self.n}건 완료{suffix}"
        _out(("\r" + line + " " * 20) if self._tty else line)


# ---------------------------------------------------------------------------
# 실패 처리
# ---------------------------------------------------------------------------


def _fail(message: str, code: int = EXIT_FAIL) -> None:
    _err("")
    for line in str(message).splitlines() or [""]:
        _err(line)
    raise typer.Exit(code)


@contextmanager
def _error_boundary(log) -> Iterator[None]:
    """명령 본문의 예외를 사용자 메시지 + 종료코드로 옮긴다.

    상세(트레이스백)는 로그 파일에만 남긴다 — 콘솔에는 다음 행동을 알 수 있는
    한 줄이면 충분하고, 트레이스백에 토큰이 섞여 나갈 여지도 줄인다.
    """
    try:
        yield
    except typer.Exit:
        raise
    except typer.Abort:
        _err("")
        _err("사용자가 중단했습니다.")
        raise typer.Exit(EXIT_FAIL) from None
    except KeyboardInterrupt:
        _err("")
        _err("중단됨(Ctrl+C).")
        raise typer.Exit(EXIT_FAIL) from None
    except AlreadyRunning as exc:
        _fail(str(exc), EXIT_LOCK)
    except TokenNotFound as exc:
        _fail(str(exc), EXIT_CONFIG)
    except DoorayApiError as exc:
        log.error("API 오류: %s", exc)
        log.debug("API 오류 상세", exc_info=True)
        _fail(f"API 오류: {exc}", EXIT_FAIL)
    except Exception as exc:  # noqa: BLE001 — 최상위 경계
        log.exception("명령 실행 실패")
        _fail(f"실패: {type(exc).__name__}: {exc}", EXIT_FAIL)


# ---------------------------------------------------------------------------
# 공통 준비
# ---------------------------------------------------------------------------


def _load_profile(name: str) -> Profile:
    try:
        return load_config(name)
    except FileNotFoundError as exc:
        _fail(str(exc), EXIT_CONFIG)
    except ValueError as exc:
        _fail(f"설정이 올바르지 않습니다: {exc}", EXIT_CONFIG)
    raise AssertionError("도달 불가")


def _require_ready(p: Profile) -> None:
    """push/pull이 성립하는 최소 설정인지."""
    problems: list[str] = []
    if not p.drive_id:
        problems.append("drive_id가 비어 있습니다")
    if not p.local_root:
        problems.append("local_root가 비어 있습니다")
    if problems:
        _fail(
            "설정이 불완전합니다:\n  - " + "\n  - ".join(problems)
            + f"\n\n  'dsync init --profile {p.name}' 을 먼저 실행하세요.",
            EXIT_CONFIG,
        )


def _token() -> str:
    try:
        return get_token()
    except TokenNotFound as exc:
        _fail(str(exc), EXIT_CONFIG)
    raise AssertionError("도달 불가")


@contextmanager
def _drive_api(p: Profile, log) -> Iterator[DriveAPI]:
    token = _token()
    with DoorayClient(p.base_url, token, logger=log) as client:
        yield DriveAPI(client)


@contextmanager
def _instance_lock(profile_name: str) -> Iterator[None]:
    """단일 인스턴스 잠금(규약 §11). 실패 시 종료코드 3.

    dry-run에도 건다 — 다른 인스턴스가 동시에 쓰는 중이면 계획 자체가 사실과
    달라지므로, "아무것도 바꾸지 않는다"는 보장보다 "정확한 계획"이 중요하다.
    """
    lock = SingleInstanceLock(lock_path(profile_name))
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        _fail(f"{exc}\n\n  다른 dsync 프로세스가 끝난 뒤 다시 실행하세요.", EXIT_LOCK)
    try:
        yield
    finally:
        lock.release()


def _rel_from_remote(full_path: str, remote_prefix: str = "") -> str:
    """원격 전체 경로 → 동기화 루트 기준 상대경로. 정본은 core.remote."""
    return rel_from_remote(full_path, remote_prefix)


def _resolve_remote_root(drive: DriveAPI, drive_id: str, remote_path: str,
                         *, create: bool = False, log=None) -> tuple[str, str]:
    """동기화 시작 폴더를 정한다. (folder_id, 정규화된 원격 접두) 반환.

    구현은 core.remote.resolve_remote_root에 있다(대소문자 무시 탐색 포함).
    여기서는 예외를 CLI 종료코드로 옮기는 일만 한다.
    """
    def _notify(name: str) -> None:
        _out(f"  원격 폴더 생성: {name}")
        if log:
            log.info("원격 폴더 생성: %s", name)

    try:
        return resolve_remote_root(drive, drive_id, remote_path, create=create,
                                   on_create=_notify)
    except RemoteRootError as exc:
        _fail(str(exc), EXIT_CONFIG)
    raise AssertionError("도달 불가")


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.stat(ext_path(path))
    except OSError:
        return None


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _collect_drives(drive: DriveAPI) -> list[dict]:
    """개인 + 프로젝트(비공개/공개) 드라이브를 한 목록으로. id 기준 중복 제거.

    실측(01_auth_drives): type=private / type=project&scope=private|public 3종이
    각각 다른 목록을 준다. 한 번의 호출로는 전부 보이지 않는다.
    """
    seen: dict[str, dict] = {}
    for kwargs in (
        {"type_": "private"},
        {"type_": "project", "scope": "private"},
        {"type_": "project", "scope": "public"},
    ):
        try:
            items = drive.list_drives(**kwargs)
        except DoorayApiError:
            # 기관 설정에 따라 특정 조합이 막혀 있을 수 있다 — 조회된 것만 쓴다.
            continue
        for d in items:
            did = str(d.get("id") or "")
            if did and did not in seen:
                merged = dict(d)
                merged.setdefault("_query", kwargs.get("scope") or kwargs["type_"])
                seen[did] = merged
    return list(seen.values())


def _drive_label(d: dict) -> str:
    name = d.get("name") or d.get("title") or "(이름 없음)"
    kind = d.get("type") or d.get("_query") or ""
    return f"{name}" + (f" [{kind}]" if kind else "")


def _choose_drive(drive: DriveAPI, preset: str) -> str:
    drives = _collect_drives(drive)
    if not drives:
        _fail("접근 가능한 드라이브가 없습니다. 토큰 권한과 IP ACL 설정을 확인하세요.", EXIT_CONFIG)

    if preset:
        for d in drives:
            if str(d.get("id")) == preset:
                _out(f"  드라이브: {_drive_label(d)} ({preset})")
                return preset
        # 목록에 없어도 접근은 될 수 있다(목록 API 필터 밖). 경고만 하고 진행한다.
        _err(f"경고: --drive-id {preset} 가 조회된 목록에 없습니다. 그대로 사용합니다.")
        return preset

    _out("")
    _table(
        ["#", "드라이브", "id"],
        [[i + 1, _drive_label(d), d.get("id")] for i, d in enumerate(drives)],
    )
    if not sys.stdin.isatty():
        _fail(
            "대화형 입력이 불가능한 환경입니다. --drive-id 로 지정하세요.",
            EXIT_CONFIG,
        )
    while True:
        raw = typer.prompt("\n동기화할 드라이브 번호").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(drives):
            return str(drives[int(raw) - 1].get("id"))
        _err(f"1~{len(drives)} 사이의 번호를 입력하세요.")


def _resolve_local_root(preset: str) -> Path:
    raw = preset.strip()
    if not raw:
        if not sys.stdin.isatty():
            _fail("대화형 입력이 불가능한 환경입니다. --local-root 로 지정하세요.", EXIT_CONFIG)
        raw = typer.prompt("로컬 동기화 루트 경로").strip().strip('"')
    if not raw:
        _fail("로컬 루트가 비어 있습니다.", EXIT_CONFIG)
    root = Path(raw)
    if not root.is_absolute():
        _fail(f"로컬 루트는 절대경로여야 합니다: {raw}", EXIT_CONFIG)
    return root


def _init_files_table(
    store: Store, drive: DriveAPI, drive_id: str, log, remote_path: str = "",
    *, create_remote: bool = False,
) -> tuple[int, int, int, list[str]]:
    """원격 walk 결과로 files 테이블 구축(원격 상태만 기록, 파일은 받지 않는다).

    반환: (기록 건수, 폴더 수, unsyncable 수, 경로키 충돌 목록)
    """
    root_id, prefix = _resolve_remote_root(drive, drive_id, remote_path,
                                           create=create_remote, log=log)
    log.info("원격 동기화 시작 폴더: %s (%s)", root_id, prefix or "드라이브 루트")

    progress = _Progress("원격 항목 스캔", every=100)
    seen_keys: dict[str, str] = {}
    collisions: list[str] = []
    bad_dirs: set[str] = set()
    n_dirs = 0
    n_unsyncable = 0
    ts = now_iso()

    # 원격 순회(네트워크)와 DB 쓰기를 분리한다. walk를 트랜잭션 안에서 돌리면
    # BEGIN IMMEDIATE가 수 분간 유지돼 다른 창의 push/pull이 database is locked로 죽는다.
    prev_records = store.all_by_key(drive_id)
    pending: list[FileRecord] = []

    for rf, full in drive.walk(drive_id, root_id, base_path=("/" + prefix) if prefix else ""):
        rel = _rel_from_remote(full, prefix)
        if not rel:
            continue
        key = path_key(rel)
        progress.tick()

        prev_seen = seen_keys.get(key)
        if prev_seen is not None:
            # 대소문자/정규화만 다른 이름은 Windows에서 공존할 수 없다.
            collisions.append(f"{rel}  (이미 {prev_seen})")
            continue
        seen_keys[key] = rel

        parent_key = key.rpartition("/")[0]
        issue = None
        if parent_key and parent_key in bad_dirs:
            issue = "상위 폴더가 Windows에 저장 불가"
        else:
            issue = name_issue(rf.name)
        if issue and rf.is_dir:
            bad_dirs.add(key)
        if issue:
            n_unsyncable += 1

        if rf.is_dir:
            n_dirs += 1

        # 재실행(--force) 시 기존 로컬 기준선을 파괴하지 않는다. upsert_file은 부분
        # 갱신이 아니라 전체 상태 반영이라, 넘기지 않은 컬럼은 NULL로 덮인다.
        # local_*를 날리면 다음 push가 모든 파일을 '변경됨'으로 보고 원격을 덮어쓴다.
        old = prev_records.get(key)
        keep = old is not None and not old.is_dir and not rf.is_dir and old.local_md5
        if issue:
            status = "unsyncable"
        elif keep:
            status = "synced"
        else:
            status = "pending_download"

        pending.append(FileRecord(
            drive_id=drive_id,
            rel_path=rel,
            file_id=rf.id or None,
            parent_id=rf.parent_id or None,
            # R14: 서버 저장명이 정본. 로컬 파일명을 쓰지 않는다(규약 §12-7).
            server_name=rf.name or None,
            is_dir=rf.is_dir,
            local_mtime_ns=old.local_mtime_ns if keep else None,
            local_size=old.local_size if keep else None,
            local_md5=old.local_md5 if keep else None,
            remote_revision=rf.revision or None,
            remote_version=rf.version,
            remote_md5=rf.md5 or (old.remote_md5 if keep else None),
            remote_size=rf.size,
            sync_status=status,
            error_msg=issue,
            last_synced_at=ts,
        ))

    # 순회가 끝난 뒤에야 쓰기 락을 잡는다 — 반쪽짜리 base 방지는 그대로 유지된다.
    with store.transaction():
        for rec in pending:
            store.upsert_file(rec)
    progress.done()
    return len(seen_keys), n_dirs, n_unsyncable, collisions


@app.command()
def init(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    drive_id: str = typer.Option("", "--drive-id", help="드라이브 id(비대화형 지정)"),
    local_root: str = typer.Option("", "--local-root", help="로컬 동기화 루트(절대경로)"),
    remote_path: str = typer.Option(
        "", "--remote-path",
        help="동기화할 원격 하위 폴더(예: 'WORK/2026'). 비우면 드라이브 전체"),
    create_remote: bool = typer.Option(
        False, "--create-remote", help="--remote-path의 원격 폴더가 없으면 새로 만듦"),
    base_url: str = typer.Option("", "--base-url", help="API base URL(기본: 공공 클라우드)"),
    force: bool = typer.Option(False, "--force", help="기존 프로파일 설정을 덮어씀"),
    dry_run: bool = typer.Option(False, "--dry-run", help="계획만 출력하고 아무것도 바꾸지 않음"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """토큰 확인 → 드라이브 선택 → 로컬 루트 지정 → 설정/DB 생성 → 원격 상태 수집."""
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        _section(f"init (profile={profile})")

        if config_exists(profile) and not force:
            _fail(
                f"프로파일 '{profile}' 설정이 이미 있습니다: {config_path()}\n"
                "  덮어쓰려면 --force 를 붙이세요. 현재 상태는 'dsync status'로 확인할 수 있습니다.",
                EXIT_CONFIG,
            )

        token = _token()
        _out(f"  토큰: {mask(token)}")

        p = Profile(name=profile)
        if config_exists(profile):
            p = _load_profile(profile)     # 기존 값을 토대로 덮어쓴다(exclude 등 보존)
        if base_url.strip():
            p.base_url = base_url.strip().rstrip("/")
        _out(f"  base_url: {p.base_url}")

        with _drive_api(p, log) as drive:
            chosen = _choose_drive(drive, drive_id.strip() or p.drive_id)
            root = _resolve_local_root(local_root or p.local_root)

            p.drive_id = chosen
            p.local_root = str(root)
            if remote_path.strip():
                p.remote_path = remote_path.strip().replace("\\", "/").strip("/")
            if not p.exclude:
                p.exclude = list(DEFAULT_EXCLUDE)

            _section("설정")
            _kv([
                ("프로파일", p.name),
                ("base_url", p.base_url),
                ("drive_id", p.drive_id),
                ("원격 폴더", p.remote_path or "(드라이브 전체)"),
                ("local_root", p.local_root),
                ("설정 파일", config_path()),
                ("상태 디렉터리", state_dir(p.name)),
                ("DB", db_path(p.name)),
                ("exclude", ", ".join(p.exclude)),
            ])

            if dry_run:
                _section("dry-run")
                _out("  설정 저장·DB 생성·원격 스캔·커서 전진을 모두 건너뜁니다.")
                raise typer.Exit(EXIT_OK)

            os.makedirs(ext_path(root), exist_ok=True)
            save_config(p)
            _out("")
            _out(f"  설정 저장 완료: {config_path()}")

            with Store(db_path(p.name)) as store:
                _section("원격 상태 수집 (파일은 받지 않습니다)")
                total, n_dirs, n_unsync, collisions = _init_files_table(
                    store, drive, p.drive_id, log, p.remote_path,
                    create_remote=create_remote,
                )
                _kv([
                    ("기록", f"{total}건 (폴더 {n_dirs}, 파일 {total - n_dirs})"),
                    ("unsyncable", f"{n_unsync}건"),
                    ("경로키 충돌", f"{len(collisions)}건"),
                ])
                for c in collisions[:20]:
                    _err(f"  경고: 경로키 충돌로 제외 — {c}")
                if len(collisions) > 20:
                    _err(f"  경고: 외 {len(collisions) - 20}건")

                _section("changes 커서 전진 (과거 이력은 재생하지 않습니다)")
                before = store.get_cursor()
                tip = drive.advance_to_tip(p.drive_id, before)
                store.set_cursor(tip)
                store.set_meta(META_LAST_FULL_SCAN, now_iso())
                _kv([("커서", f"revision={tip.revision} file_id={tip.file_id or '-'}")])

        _out("")
        _out("초기화 완료. 다음: 'dsync pull --dry-run' 또는 'dsync push --dry-run'")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command()
def status(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    dry_run: bool = typer.Option(False, "--dry-run", help="(status는 읽기 전용 — 동작 동일)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """설정 요약, sync_status별 건수, 커서, 마지막 동기화 시각, 미해결 충돌 수."""
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        p = _load_profile(profile)

        _section(f"설정 (profile={p.name})")
        root_exists = os.path.isdir(ext_path(p.root_path)) if p.local_root else False
        _kv([
            ("base_url", p.base_url),
            ("drive_id", p.drive_id or "(미설정)"),
            ("local_root", f"{p.local_root or '(미설정)'}" + ("" if root_exists else "  ← 없음")),
            ("설정 파일", config_path()),
            ("로그", current_log_path() or "(파일 로그 없음)"),
            ("propagate_deletes", p.propagate_deletes),
            ("exclude", ", ".join(p.exclude)),
        ])

        _section("토큰")
        try:
            _kv([("keyring/환경변수", mask(get_token()))])
        except TokenNotFound:
            _err("  토큰 없음 — 'dsync doctor' 로 등록 방법을 확인하세요.")

        db = db_path(p.name)
        if not os.path.exists(ext_path(db)):
            _section("상태 DB")
            _err(f"  DB가 없습니다: {db}")
            _err("  'dsync init' 을 먼저 실행하세요.")
            raise typer.Exit(EXIT_CONFIG)

        with Store(db) as store:
            counts = store.count_by_status(p.drive_id)
            total = store.count_files(p.drive_id)
            cursor = store.get_cursor()
            conflicts = list(store.iter_unresolved())
            incomplete = list(store.iter_incomplete())

            _section("상태 DB")
            _kv([("경로", db), ("총 항목", f"{total}건")])
            if counts:
                _out("")
                _table(
                    ["sync_status", "건수"],
                    [[k, v] for k, v in sorted(counts.items(), key=lambda kv: -kv[1])],
                )

            _section("동기화 지점")
            _kv([
                ("changes 커서", f"revision={cursor.revision} file_id={cursor.file_id or '-'}"),
                ("마지막 전체 스캔", store.get_meta(META_LAST_FULL_SCAN) or "-"),
                ("마지막 push", store.get_meta(META_LAST_PUSH_AT) or "-"),
                ("마지막 pull", store.get_meta(META_LAST_PULL_AT) or "-"),
                ("미해결 충돌", f"{len(conflicts)}건"),
                ("미완료 저널", f"{len(incomplete)}건"),
            ])
            for c in conflicts[:10]:
                _out(f"    충돌: {c['rel_path']}  ({c['kind']}, {c['ts']})")
            for j in incomplete[:10]:
                _out(f"    미완료: {j['op']} {j['rel_path'] or ''} phase={j['phase']} ({j['ts']})")
        _out("")


# ---------------------------------------------------------------------------
# push — 로컬 → 원격 (원격 무손상)
# ---------------------------------------------------------------------------


@dataclass
class _PushItem:
    op: str                 # 'FOLDER' | 'NEW' | 'UPDATE' | 'TOUCH'
    rel: str
    is_dir: bool = False
    size: int | None = None
    mtime_ns: int | None = None
    md5: str | None = None
    note: str = ""
    # 디스크 원본 표기 경로. rel은 NFC로 정규화돼 있어 NFD 이름의 파일을 열 수 없다.
    disk_path: str = ""


_PUSH_LABEL = {
    "FOLDER": "폴더생성",
    "NEW": "신규업로드",
    "UPDATE": "새버전",
    "TOUCH": "기록갱신",
}


@dataclass
class _PushPlan:
    items: list[_PushItem] = field(default_factory=list)
    skipped_same: int = 0
    local_missing: int = 0
    warnings: list[str] = field(default_factory=list)
    # 해시를 읽지 못해 계획에서 빠진 파일. 경고로만 흘리면 "올리지 못했는데 종료코드 0"이
    # 되므로 호출측이 DB error 기록 + 실패 집계에 반드시 반영한다.
    hash_failures: list[tuple[str, str]] = field(default_factory=list)
    # 원격에는 있는데 로컬 기준선(local_md5)이 없어 어느 쪽이 최신인지 알 수 없는 파일.
    # 올리면 원격을 덮어쓰므로 기본은 보류한다.
    ambiguous: list[str] = field(default_factory=list)


def _plan_push(scanner: LocalScanner, entries: dict, base: dict, p: Profile, log,
               *, assume_local_newer: bool = False) -> _PushPlan:
    """로컬 스냅샷과 DB base를 비교해 업로드 계획을 만든다. API 호출 없음.

    원격 존재 여부는 실행 단계에서 부모 폴더 목록으로 재확인한다(D1) — 그래서
    'NEW'가 실행 중 'UPDATE'로 바뀔 수 있고, 그 경우 note에 근거를 남긴다.
    """
    plan = _PushPlan()
    warn_bytes = max(0, int(p.max_file_mb_warn)) * 1024 * 1024

    for key, entry in entries.items():
        rec = base.get(key)
        if entry.is_dir:
            if rec is None or not rec.is_dir or not rec.file_id:
                plan.items.append(_PushItem(op="FOLDER", rel=entry.rel_path, is_dir=True,
                                            disk_path=entry.disk_path))
            continue

        note = ""
        if server_name_will_differ(entry.rel_path.rpartition("/")[2]):
            # R14: 서버가 앞뒤 공백을 절삭하고 '"'를 '%22'로 바꾼다. 업로드 응답의
            # 저장명을 정본으로 기록해야 매 실행 재전송 루프에 빠지지 않는다.
            note = "서버가 이름을 변경할 수 있음(R14)"

        unchanged = (
            rec is not None
            and not rec.is_dir
            and rec.file_id
            and rec.local_mtime_ns is not None
            and rec.local_size is not None
            and int(rec.local_mtime_ns) == (entry.mtime_ns or -1)
            and int(rec.local_size) == (entry.size or -1)
        )
        if unchanged:
            plan.skipped_same += 1
            continue

        # (mtime,size)가 달라졌을 때만 해시한다 — 스캔 비용의 대부분이 해시 IO다.
        if scanner.needs_hash(entry, rec):
            try:
                entry = scanner.fill_md5(entry)
            except OSError as exc:
                # 오피스/한글이 배타 잠금한 파일, 백신 잠금, 권한 거부가 여기로 온다.
                # 계획에서 빼되 '실패'로 기록해야 종료코드·status에 드러난다.
                plan.hash_failures.append(
                    (entry.rel_path, f"해시 실패 — {type(exc).__name__}: {exc}"))
                continue

        if rec is not None and not rec.is_dir and rec.file_id and rec.local_md5 == entry.md5:
            # 내용은 그대로고 타임스탬프만 바뀐 경우 — 전송하지 않고 base만 맞춘다.
            plan.items.append(_PushItem(
                op="TOUCH", rel=entry.rel_path, size=entry.size, disk_path=entry.disk_path,
                mtime_ns=entry.mtime_ns, md5=entry.md5, note="내용 동일(mtime만 변경)",
            ))
            continue

        # init 직후처럼 원격 레코드는 있는데 로컬 기준선이 없는 상태에서 UPDATE를 내면,
        # 로컬이 원격보다 오래됐어도 그대로 덮어쓴다. 목록 API에는 hash가 없어(실측)
        # 원격 내용과 비교할 방법도 없다 → 기본은 보류하고 pull을 먼저 권한다.
        if (rec is not None and rec.file_id and not rec.is_dir
                and not rec.local_md5 and not assume_local_newer):
            plan.ambiguous.append(entry.rel_path)
            continue

        if warn_bytes and (entry.size or 0) >= warn_bytes:
            note = (note + " / " if note else "") + f"대용량({_human_size(entry.size)})"

        plan.items.append(_PushItem(
            op=("UPDATE" if (rec is not None and rec.file_id and not rec.is_dir) else "NEW"),
            rel=entry.rel_path, size=entry.size, mtime_ns=entry.mtime_ns,
            md5=entry.md5, note=note, disk_path=entry.disk_path,
        ))

    for key, rec in base.items():
        if key not in entries and not rec.is_dir:
            plan.local_missing += 1
    return plan


class _PushExecutor:
    """계획 실행. 원격 삭제·이동은 어떤 경로로도 하지 않는다."""

    def __init__(self, drive: DriveAPI, store: Store, p: Profile, base: dict, log) -> None:
        self.drive = drive
        self.store = store
        self.p = p
        self.base = base
        self.log = log
        self.root = p.root_path
        self.drive_id = p.drive_id
        self.root_id, _ = _resolve_remote_root(drive, p.drive_id, p.remote_path)
        self.folder_ids: dict[str, str] = {"": self.root_id}
        self._index: dict[str, dict[str, RemoteFile]] = {}
        # 대소문자 무시 보조 색인. 서버의 이름 중복 검사가 대소문자를 무시하므로
        # (실측 2026-08-02) 정확 일치만 보면 원격 'WRITING'을 로컬 'Writing'으로 찾지 못하고
        # 새로 만들려다 409 Duplicate request로 영원히 막힌다.
        self._folded: dict[str, dict[str, RemoteFile]] = {}
        self.failures: list[tuple[str, str]] = []
        self.done: dict[str, int] = {"FOLDER": 0, "NEW": 0, "UPDATE": 0, "TOUCH": 0}

    # ---- 원격 조회 ----
    def _dir_index(self, parent_id: str) -> dict[str, RemoteFile]:
        """부모 폴더의 이름→항목 색인. 폴더당 한 번만 목록을 읽는다.

        D1 분기(동일 이름 존재 → PUT / 부재 → POST)의 근거이자, 폴더 재귀 생성의
        존재 확인 수단이다. 경쟁 상태로 색인이 낡으면 409로 드러나고 그때 재조회한다.
        """
        idx = self._index.get(parent_id)
        if idx is None:
            idx = {}
            folded: dict[str, RemoteFile] = {}
            for child in self.drive.iter_children(self.drive_id, parent_id):
                if child.sub_type == "trash":
                    continue
                name = to_nfc(child.name)
                idx[name] = child
                folded.setdefault(path_key(name), child)   # 먼저 본 쪽을 유지
            self._index[parent_id] = idx
            self._folded[parent_id] = folded
        return idx

    def _idx_find(self, parent_id: str, *names: str) -> RemoteFile | None:
        """부모 폴더 색인 조회. 주어진 이름들로 정확 일치를 먼저 보고, 없으면 대소문자 무시."""
        idx = self._dir_index(parent_id)
        for n in names:
            if n and n in idx:
                return idx[n]
        folded = self._folded.get(parent_id) or {}
        for n in names:
            key = path_key(n) if n else ""
            if key and key in folded:
                return folded[key]
        return None

    def _idx_put(self, parent_id: str, rf: RemoteFile) -> None:
        """새로 만들거나 확인한 항목을 두 색인에 함께 반영한다."""
        name = to_nfc(rf.name)
        self._dir_index(parent_id)[name] = rf
        self._folded.setdefault(parent_id, {})[path_key(name)] = rf

    def _forget_index(self, parent_id: str) -> None:
        self._index.pop(parent_id, None)
        self._folded.pop(parent_id, None)

    def ensure_folder(self, rel: str) -> str:
        """폴더 상대경로 → 원격 folder id. 상위부터 재귀적으로 확인·생성한다."""
        key = path_key(rel)
        if key in self.folder_ids:
            return self.folder_ids[key]

        parent_rel, _, name = rel.rpartition("/")
        parent_id = self.ensure_folder(parent_rel) if parent_rel else self.root_id

        rec = self.base.get(key)
        lookup = to_nfc(rec.server_name) if (rec and rec.server_name) else ""
        found = self._idx_find(parent_id, lookup, to_nfc(name))

        if found is not None and not found.is_dir:
            raise DoorayApiError(
                f"원격에 같은 이름의 파일이 있어 폴더를 만들 수 없습니다: {rel}"
            )
        if found is not None:
            fid = found.id
            server_name = found.name
        else:
            # R14: 서버 저장명을 정본으로 받는다. 로컬 이름을 기록하면 다음 push가
            # 같은 폴더를 다시 만들려 하고 파일들이 엉뚱한 부모로 흩어진다.
            # create_folder_full은 409(대소문자만 다른 폴더 존재 포함)를 기존 폴더 반환으로
            # 흡수하므로, 여기서 새로 만든 것인지 찾은 것인지는 id로 구분하지 않는다.
            created, is_new = self.drive.create_folder_ex(self.drive_id, parent_id, name)
            fid = created.id
            server_name = created.name or to_nfc(name)
            self._idx_put(parent_id, created)
            if is_new:
                # 방금 만든 폴더는 비어 있다 — 목록 조회를 건너뛴다.
                # 409로 기존 폴더를 받은 경우에는 절대 비었다고 가정하면 안 된다.
                self._index[fid] = {}
                self._folded[fid] = {}
                self.done["FOLDER"] += 1

        self.folder_ids[key] = fid
        self.store.upsert_file(FileRecord(
            drive_id=self.drive_id, rel_path=rel, file_id=fid, parent_id=parent_id,
            server_name=server_name, is_dir=True,
            sync_status="synced", last_synced_at=now_iso(),
        ))
        self.base[key] = self.store.get_by_path(self.drive_id, rel) or self.base.get(key)
        return fid

    # ---- 파일 업로드 ----
    def upload(self, item: _PushItem) -> str:
        """한 파일을 업로드하고 실제로 수행한 동작('NEW'/'UPDATE')을 돌려준다."""
        rel = item.rel
        key = path_key(rel)
        rec = self.base.get(key)
        parent_rel, _, name = rel.rpartition("/")
        parent_id = self.ensure_folder(parent_rel) if parent_rel else self.root_id
        # 디스크 원본 표기로 연다 — rel은 NFC 정규화본이라 NFD 이름은 열리지 않는다.
        src = Path(item.disk_path) if item.disk_path else local_path(self.root, rel)

        lookup = to_nfc(rec.server_name) if (rec and rec.server_name) else ""
        # 대소문자만 다른 동명 항목도 서버에는 '같은 이름'이다(실측) — 정확 일치가 없으면
        # 대소문자 무시로 한 번 더 본다. 못 찾고 POST하면 409로 막힌다.
        found = self._idx_find(parent_id, lookup, to_nfc(name))

        op = "UPDATE" if (found is not None and not found.is_dir) else "NEW"
        if found is not None and found.is_dir:
            raise DoorayApiError(f"원격에 같은 이름의 폴더가 있어 업로드할 수 없습니다: {rel}")

        if op == "UPDATE":
            res = self.drive.upload_version(self.drive_id, found.id, name, src)
            file_id = str(res.get("id") or found.id)
            version = res.get("version")
            server_name = found.name           # 정본은 서버가 준 이름(R14)
        else:
            try:
                rf = self.drive.upload_new(self.drive_id, parent_id, name, src)
            except DoorayApiError as exc:
                if exc.status != 409:
                    raise
                # D1: 409 = 순수 이름 충돌(경쟁 상태). 색인을 버리고 재조회 후 재판정.
                # find_child_by_name은 대소문자 무시 폴백을 포함하므로, 서버가 같은 이름으로
                # 보는 항목을 여기서 반드시 찾아낸다(못 찾으면 원인 없는 409가 반복된다).
                self.log.warning("409 이름 충돌 → 재조회 후 새 버전으로 전환: %s", rel)
                self._forget_index(parent_id)
                existing = self.drive.find_child_by_name(self.drive_id, parent_id, name)
                if existing is None or existing.is_dir:
                    raise
                res = self.drive.upload_version(self.drive_id, existing.id, name, src)
                file_id = str(res.get("id") or existing.id)
                version = res.get("version")
                server_name = existing.name
                self._idx_put(parent_id, existing)
                self._commit(rel, file_id, parent_id, server_name, version, item)
                return "UPDATE"
            file_id = rf.id
            version = rf.version
            server_name = rf.name or to_nfc(name)   # R14: 업로드 응답의 저장명이 정본
            self._idx_put(parent_id, dataclasses.replace(rf, name=server_name))

        self._commit(rel, file_id, parent_id, server_name, version, item)
        return op

    def _commit(self, rel: str, file_id: str, parent_id: str, server_name: str,
                version, item: _PushItem) -> None:
        """전송 후 base 갱신. 전송 도중 로컬이 또 바뀌었으면 다음 실행이 잡도록 표시한다."""
        st = _stat_or_none(Path(item.disk_path) if item.disk_path else local_path(self.root, rel))
        moved = st is not None and (st.st_mtime_ns != item.mtime_ns or st.st_size != item.size)
        if server_name and to_nfc(server_name) != to_nfc(rel.rpartition("/")[2]):
            _err(f"  알림: 서버 저장명이 다릅니다 — 로컬 '{rel.rpartition('/')[2]}' → 서버 '{server_name}' (R14)")

        self.store.upsert_file(FileRecord(
            drive_id=self.drive_id, rel_path=rel, file_id=file_id or None,
            parent_id=parent_id or None, server_name=server_name or None, is_dir=False,
            local_mtime_ns=item.mtime_ns, local_size=item.size, local_md5=item.md5,
            remote_version=version if isinstance(version, int) else None,
            # 업로드 직후 원격 내용 = 방금 올린 로컬 내용. MD5는 동일하다(실측 확인된 알고리즘).
            remote_md5=item.md5, remote_size=item.size,
            sync_status="pending_upload" if moved else "synced",
            error_msg="전송 중 로컬 파일이 변경됨 — 다음 push에서 재전송" if moved else None,
            last_synced_at=now_iso(),
        ))
        self.base[path_key(rel)] = self.store.get_by_path(self.drive_id, rel)

    def touch(self, item: _PushItem) -> None:
        """내용이 같고 타임스탬프만 바뀐 파일 — 전송 없이 base만 맞춘다."""
        rec = self.base.get(path_key(item.rel))
        self.store.upsert_file(FileRecord(
            drive_id=self.drive_id, rel_path=item.rel,
            file_id=rec.file_id if rec else None,
            parent_id=rec.parent_id if rec else None,
            server_name=rec.server_name if rec else None, is_dir=False,
            local_mtime_ns=item.mtime_ns, local_size=item.size, local_md5=item.md5,
            remote_revision=rec.remote_revision if rec else None,
            remote_version=rec.remote_version if rec else None,
            remote_md5=rec.remote_md5 if rec else None,
            remote_size=rec.remote_size if rec else None,
            sync_status="synced", last_synced_at=now_iso(),
        ))
        self.done["TOUCH"] += 1


@app.command()
def push(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    dry_run: bool = typer.Option(False, "--dry-run", help="계획만 출력하고 아무것도 바꾸지 않음"),
    assume_local_newer: bool = typer.Option(
        False, "--assume-local-newer",
        help="로컬 기준선이 없는 파일도 로컬이 최신이라고 보고 업로드(원격을 덮어씀)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """로컬 → 원격 단방향 업로드. 원격의 어떤 것도 삭제·이동하지 않습니다."""
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        p = _load_profile(profile)
        _require_ready(p)

        with _instance_lock(p.name):
            _section(f"push (profile={p.name}{', dry-run' if dry_run else ''})")
            root = p.root_path
            if not os.path.isdir(ext_path(root)):
                _fail(f"로컬 루트가 없습니다: {root}", EXIT_CONFIG)

            with Store(db_path(p.name)) as store:
                scanner = LocalScanner(root, p.exclude, logger=log)
                _out("  로컬 스캔 중...")
                entries = scanner.scan()
                base = store.all_by_key(p.drive_id)
                _kv([
                    ("로컬 항목", f"{len(entries)}건"),
                    ("base 레코드", f"{len(base)}건"),
                    ("스캔 제외", f"{len(scanner.skipped)}건"),
                ])
                for s in scanner.skipped[:10]:
                    _err(f"  건너뜀: {s.rel_path} — {s.reason}")

                plan = _plan_push(scanner, entries, base, p, log,
                                  assume_local_newer=assume_local_newer)
                _print_push_plan(plan)

                if dry_run:
                    _out("")
                    _out("dry-run — 아무것도 변경하지 않았습니다.")
                    raise typer.Exit(EXIT_OK)

                # 해시를 못 읽어 계획에서 빠진 파일을 DB와 종료코드에 반영한다.
                # dry-run 이후에 두어 --dry-run이 DB를 건드리지 않도록 한다.
                for rel, reason in plan.hash_failures:
                    _record_error(store, p.drive_id, rel, False, reason)
                    log.error("계획 제외 %s: %s", rel, reason)

                if not plan.items:
                    _out("")
                    _out("업로드할 항목이 없습니다.")
                    store.set_meta(META_LAST_PUSH_AT, now_iso())
                    raise typer.Exit(EXIT_FAIL if plan.hash_failures else EXIT_OK)

                with _drive_api(p, log) as drive:
                    ex = _PushExecutor(drive, store, p, base, log)
                    _section("업로드")
                    progress = _Progress("처리", every=20)
                    for item in plan.items:
                        try:
                            if item.op == "FOLDER":
                                ex.ensure_folder(item.rel)
                            elif item.op == "TOUCH":
                                ex.touch(item)
                            else:
                                actual = ex.upload(item)
                                ex.done[actual] += 1
                                log.info("%s: %s", _PUSH_LABEL[actual], item.rel)
                        except Exception as exc:  # 파일 단위 격리 — 하나가 실패해도 계속
                            reason = f"{type(exc).__name__}: {exc}"
                            ex.failures.append((item.rel, reason))
                            log.error("실패 %s: %s", item.rel, reason)
                            log.debug("실패 상세 %s", item.rel, exc_info=True)
                            _record_error(store, p.drive_id, item.rel, item.is_dir, reason)
                        progress.tick()
                    progress.done()

                    store.set_meta(META_LAST_PUSH_AT, now_iso())
                    _push_summary(ex, plan)
                    if ex.failures or plan.hash_failures:
                        raise typer.Exit(EXIT_FAIL)


def _print_push_plan(plan: _PushPlan) -> None:
    _section("계획")
    if not plan.items:
        _out("  변경 없음")
    else:
        rows = [
            [_PUSH_LABEL.get(i.op, i.op), i.rel, "-" if i.is_dir else _human_size(i.size), i.note]
            for i in plan.items[:_MAX_PLAN_ROWS]
        ]
        _table(["동작", "경로", "크기", "비고"], rows)
        if len(plan.items) > _MAX_PLAN_ROWS:
            _out(f"  ... 외 {len(plan.items) - _MAX_PLAN_ROWS}건")
    _out("")
    _kv([
        ("변경 없음(건너뜀)", f"{plan.skipped_same}건"),
        ("로컬에서 사라짐", f"{plan.local_missing}건 — M1은 원격을 삭제하지 않습니다(보고만)"),
    ])
    if plan.ambiguous:
        _out("")
        _err(f"  보류 {len(plan.ambiguous)}건: 원격에도 있지만 로컬 기준선이 없어 "
             f"어느 쪽이 최신인지 알 수 없습니다.")
        _err("        'dsync reconcile'을 먼저 실행하세요 — 원격 내용을 받아 대조해서")
        _err("        같으면 기준선만 기록하고(전송 없음), 다르면 알려 줍니다.")
        _err("        로컬이 최신임이 확실하면 'dsync push --assume-local-newer'.")
        for rel in plan.ambiguous[:10]:
            _err(f"        - {rel}")
        if len(plan.ambiguous) > 10:
            _err(f"        ... 외 {len(plan.ambiguous) - 10}건")
    if plan.hash_failures:
        _out("")
        _err(f"  읽기 실패 {len(plan.hash_failures)}건 — 업로드하지 못했습니다:")
        for rel, reason in plan.hash_failures[:10]:
            _err(f"        - {rel}: {reason}")
    for w in plan.warnings[:20]:
        _err(f"  경고: {w}")


def _push_summary(ex: _PushExecutor, plan: _PushPlan) -> None:
    _section("결과")
    _kv([
        ("폴더 생성", f"{ex.done['FOLDER']}건"),
        ("신규 업로드", f"{ex.done['NEW']}건"),
        ("새 버전", f"{ex.done['UPDATE']}건"),
        ("기록만 갱신", f"{ex.done['TOUCH']}건"),
        ("실패", f"{len(ex.failures)}건"),
    ])
    if ex.failures:
        _out("")
        _table(["경로", "사유"], [[r, s[:120]] for r, s in ex.failures[:_MAX_PLAN_ROWS]])
        if len(ex.failures) > _MAX_PLAN_ROWS:
            _out(f"  ... 외 {len(ex.failures) - _MAX_PLAN_ROWS}건")


def _record_error(store: Store, drive_id: str, rel: str, is_dir: bool, reason: str) -> None:
    """실패를 DB에 남긴다. 기존 레코드가 있으면 상태/사유만 바꿔 base를 보존한다."""
    rec = store.get_by_path(drive_id, rel)
    if rec is None:
        rec = FileRecord(drive_id=drive_id, rel_path=rel, is_dir=is_dir)
    rec.sync_status = "error"
    rec.error_msg = reason[:500]
    store.upsert_file(rec)


# ---------------------------------------------------------------------------
# pull — 원격 → 로컬 (수정된 로컬은 절대 덮어쓰지 않음)
# ---------------------------------------------------------------------------


@dataclass
class _PullItem:
    op: str                 # 'MKDIR' | 'NEW' | 'UPDATE'
    rel: str
    rf: RemoteFile | None = None
    note: str = ""

    @property
    def size(self) -> int | None:
        return None if self.rf is None else self.rf.size


_PULL_LABEL = {"MKDIR": "폴더생성", "NEW": "신규받기", "UPDATE": "갱신받기"}


class _LocalChangedDuringPull(RuntimeError):
    """전송 중 로컬이 바뀌어 교체를 포기했다는 신호. 실패가 아니라 보호로 집계한다."""


@dataclass
class _PullPlan:
    items: list[_PullItem] = field(default_factory=list)
    up_to_date: int = 0
    unsyncable: list[tuple[str, str]] = field(default_factory=list)
    protected: list[tuple[str, str]] = field(default_factory=list)  # 로컬 수정 → 보호(skip)
    remote_missing: int = 0


def _local_modified(entry, rec: FileRecord | None, scanner: LocalScanner) -> str | None:
    """로컬 파일이 base와 다른지. 다르면 사유 문자열(= 덮어쓰기 금지 근거), 같으면 None.

    C2의 근간이다. base를 모르는 파일(init 직후 등)은 **모른다 = 위험**으로 보고
    보호한다 — 확인되지 않은 파일을 덮어쓰는 것이 이 프로그램에서 가장 큰 사고다.
    """
    if rec is None or rec.local_md5 is None:
        return "기준선 없음 — 'dsync reconcile'로 원격과 대조하세요"
    if entry.mtime_ns is None or entry.size is None:
        return "로컬 메타를 읽을 수 없음"
    if (rec.local_mtime_ns is not None and rec.local_size is not None
            and int(rec.local_mtime_ns) == entry.mtime_ns
            and int(rec.local_size) == entry.size):
        return None
    try:
        entry = scanner.fill_md5(entry)
    except OSError as exc:
        return f"해시 실패 — {type(exc).__name__}: {exc}"
    return None if entry.md5 == rec.local_md5 else "로컬에서 수정됨"


def _plan_pull(drive: DriveAPI, scanner: LocalScanner, entries: dict, base: dict,
               p: Profile, log) -> _PullPlan:
    plan = _PullPlan()
    root_id, prefix = _resolve_remote_root(drive, p.drive_id, p.remote_path)
    progress = _Progress("원격 항목 스캔", every=100)
    seen: set[str] = set()
    bad_dirs: set[str] = set()
    # file_id 역색인. 서버가 업로드 시 이름을 바꾸면(R14: 앞뒤 공백 절삭) 원격 경로가
    # base의 어떤 키와도 안 맞아 '신규'로 보인다. 그대로 받으면 같은 원격 파일이
    # 로컬에 두 벌 생기고 이후 서로를 덮어쓴다. id로 한 번 더 확인해서 막는다.
    by_file_id = {r.file_id: r for r in base.values() if r.file_id and not r.is_dir}

    for rf, full in drive.walk(p.drive_id, root_id, base_path=("/" + prefix) if prefix else ""):
        rel = _rel_from_remote(full, prefix)
        if not rel:
            continue
        progress.tick()
        key = path_key(rel)
        if key in seen:
            plan.unsyncable.append((rel, "경로키 충돌(대소문자/정규화만 다른 이름)"))
            continue
        seen.add(key)

        if scanner.is_excluded(rel):
            continue

        parent_key = key.rpartition("/")[0]
        if parent_key and parent_key in bad_dirs:
            if rf.is_dir:
                bad_dirs.add(key)
            plan.unsyncable.append((rel, "상위 폴더가 Windows에 저장 불가"))
            continue
        issue = name_issue(rf.name)
        if issue:
            if rf.is_dir:
                bad_dirs.add(key)
            plan.unsyncable.append((rel, issue))
            continue

        rec = base.get(key)
        entry = entries.get(key)

        if rf.is_dir:
            if entry is None:
                plan.items.append(_PullItem(op="MKDIR", rel=rel, rf=rf))
            elif not entry.is_dir:
                plan.protected.append((rel, "원격은 폴더, 로컬은 파일 — 건드리지 않음"))
            else:
                plan.up_to_date += 1
            continue

        if entry is not None and entry.is_dir:
            plan.protected.append((rel, "원격은 파일, 로컬은 폴더 — 건드리지 않음"))
            continue

        if entry is not None:
            why = _local_modified(entry, rec, scanner)
            if why is not None:
                # C2: 수정된(또는 확인 불가한) 로컬 파일은 절대 덮어쓰지 않는다.
                plan.protected.append((rel, why))
                continue
            same_remote = (
                rec is not None
                and rec.remote_version == rf.version
                and (rec.remote_size is None or rf.size is None or rec.remote_size == rf.size)
            )
            if same_remote:
                plan.up_to_date += 1
                continue
            plan.items.append(_PullItem(op="UPDATE", rel=rel, rf=rf,
                                        note=f"version {rec.remote_version if rec else '-'} → {rf.version}"))
            continue

        twin = by_file_id.get(rf.id) if rf.id else None
        if twin is not None and path_key(twin.rel_path) != key:
            # 같은 원격 파일이 base에 다른 로컬 경로로 이미 잡혀 있다(서버 개명 등).
            # 새로 받으면 중복본이 생기므로 받지 않고 보고만 한다.
            plan.protected.append((
                rel, f"같은 원격 파일이 로컬 '{twin.rel_path}'로 이미 기록됨 — 중복 생성 방지"))
            continue

        plan.items.append(_PullItem(op="NEW", rel=rel, rf=rf))

    progress.done()
    for key, rec in base.items():
        if key not in seen and rec.file_id:
            plan.remote_missing += 1
    return plan


def _execute_pull(drive: DriveAPI, store: Store, scanner: LocalScanner, entries: dict,
                  base: dict, plan: _PullPlan, p: Profile, log) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    root = p.root_path
    done = {"MKDIR": 0, "NEW": 0, "UPDATE": 0, "SKIP": 0}
    progress = _Progress("처리", every=20)

    for item in plan.items:
        rel = item.rel
        rf = item.rf
        dest = local_path(root, rel)
        try:
            if item.op == "MKDIR":
                os.makedirs(ext_path(dest), exist_ok=True)
                store.upsert_file(FileRecord(
                    drive_id=p.drive_id, rel_path=rel, file_id=rf.id or None,
                    parent_id=rf.parent_id or None, server_name=rf.name or None, is_dir=True,
                    remote_revision=rf.revision or None, remote_version=rf.version,
                    sync_status="synced", last_synced_at=now_iso(),
                ))
                done["MKDIR"] += 1
                progress.tick()
                continue

            # C2: 전송 시작 전 1차 확인. 스캔 시점과 다르면 그 사이 로컬이 바뀐 것이므로
            # 검증 없이 덮어쓰지 않는다(규약 §12-6).
            entry = entries.get(path_key(rel))
            st = _stat_or_none(dest)
            if entry is not None:
                if st is None:
                    log.info("스캔 후 로컬 파일이 사라짐 — 신규로 받습니다: %s", rel)
                elif st.st_mtime_ns != entry.mtime_ns or st.st_size != entry.size:
                    plan.protected.append((rel, "다운로드 직전 로컬이 변경됨(재-stat 불일치)"))
                    done["SKIP"] += 1
                    progress.tick()
                    continue
            elif st is not None:
                # 스캔에는 없었는데 지금은 있다 = 방금 만들어진 파일. 덮어쓰지 않는다.
                plan.protected.append((rel, "스캔 이후 로컬에 새로 생김 — 덮어쓰지 않음"))
                done["SKIP"] += 1
                progress.tick()
                continue

            # C2 본검사: 전송에 걸린 시간(대용량은 수 분) 동안 사용자가 저장했을 수
            # 있으므로 os.replace 바로 직전에 한 번 더 본다. 여기서 걸리면 받은 내용을
            # 버리고 로컬을 지킨다 — 편집 손실이 '성공'으로 집계되는 것을 막는 마지막 방어선.
            expect = (st.st_mtime_ns, st.st_size) if st is not None else None

            def _guard(_dest: Path = dest, _rel: str = rel, _expect=expect) -> None:
                now = _stat_or_none(_dest)
                if _expect is None:
                    if now is not None:
                        raise _LocalChangedDuringPull(f"전송 중 로컬에 파일이 생성됨: {_rel}")
                    return
                if now is None:
                    raise _LocalChangedDuringPull(f"전송 중 로컬 파일이 사라짐: {_rel}")
                if (now.st_mtime_ns, now.st_size) != _expect:
                    raise _LocalChangedDuringPull(f"전송 중 로컬 파일이 수정됨: {_rel}")

            os.makedirs(ext_path(dest.parent), exist_ok=True)
            info = drive.download(
                p.drive_id, rf.id, dest,
                expected_size=rf.size, expected_md5=rf.md5,
                pre_replace_guard=_guard,
            )
            md5 = str(info.get("md5") or "") or None
            fresh = _stat_or_none(dest)
            store.upsert_file(FileRecord(
                drive_id=p.drive_id, rel_path=rel, file_id=rf.id or None,
                parent_id=rf.parent_id or None,
                server_name=rf.name or None,   # R14: 서버 저장명이 정본
                is_dir=False,
                local_mtime_ns=fresh.st_mtime_ns if fresh else None,
                local_size=fresh.st_size if fresh else info.get("bytes"),
                local_md5=md5,
                remote_revision=rf.revision or None, remote_version=rf.version,
                remote_md5=rf.md5 or md5, remote_size=rf.size,
                sync_status="synced", last_synced_at=now_iso(),
            ))
            done[item.op] += 1
            log.info("%s: %s (%s)", _PULL_LABEL[item.op], rel, _human_size(info.get("bytes")))
        except _LocalChangedDuringPull as exc:
            # 실패가 아니라 '보호'다 — 로컬을 지킨 것이므로 종료코드를 실패로 만들지 않는다.
            plan.protected.append((rel, str(exc)))
            done["SKIP"] += 1
            log.warning("보호: %s", exc)
        except Exception as exc:  # 파일 단위 격리
            reason = f"{type(exc).__name__}: {exc}"
            failures.append((rel, reason))
            log.error("실패 %s: %s", rel, reason)
            log.debug("실패 상세 %s", rel, exc_info=True)
            _record_error(store, p.drive_id, rel, item.op == "MKDIR", reason)
        progress.tick()

    progress.done()
    _section("결과")
    _kv([
        ("폴더 생성", f"{done['MKDIR']}건"),
        ("신규 받기", f"{done['NEW']}건"),
        ("갱신 받기", f"{done['UPDATE']}건"),
        ("보호로 건너뜀", f"{done['SKIP']}건"),
        ("실패", f"{len(failures)}건"),
    ])
    if failures:
        _out("")
        _table(["경로", "사유"], [[r, s[:120]] for r, s in failures[:_MAX_PLAN_ROWS]])
    return failures


def _mark_unsyncable(store: Store, drive_id: str, plan: _PullPlan) -> None:
    for rel, reason in plan.unsyncable:
        rec = store.get_by_path(drive_id, rel) or FileRecord(drive_id=drive_id, rel_path=rel)
        rec.sync_status = "unsyncable"
        rec.error_msg = reason[:500]
        store.upsert_file(rec)


@app.command()
def pull(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    dry_run: bool = typer.Option(False, "--dry-run", help="계획만 출력하고 아무것도 바꾸지 않음"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """원격 → 로컬 단방향 다운로드. 수정된 로컬 파일은 절대 덮어쓰지 않습니다."""
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        p = _load_profile(profile)
        _require_ready(p)

        with _instance_lock(p.name):
            _section(f"pull (profile={p.name}{', dry-run' if dry_run else ''})")
            root = p.root_path
            if not dry_run:
                os.makedirs(ext_path(root), exist_ok=True)
            if not os.path.isdir(ext_path(root)):
                _fail(f"로컬 루트가 없습니다: {root}", EXIT_CONFIG)

            with Store(db_path(p.name)) as store:
                scanner = LocalScanner(root, p.exclude, logger=log)
                _out("  로컬 스캔 중...")
                entries = scanner.scan()
                base = store.all_by_key(p.drive_id)
                _kv([
                    ("로컬 항목", f"{len(entries)}건"),
                    ("base 레코드", f"{len(base)}건"),
                    ("스캔 제외", f"{len(scanner.skipped)}건"),
                ])

                with _drive_api(p, log) as drive:
                    plan = _plan_pull(drive, scanner, entries, base, p, log)
                    _print_pull_plan(plan)

                    if dry_run:
                        _out("")
                        _out("dry-run — 아무것도 변경하지 않았습니다.")
                        raise typer.Exit(EXIT_OK)

                    _mark_unsyncable(store, p.drive_id, plan)
                    if not plan.items:
                        _out("")
                        _out("받을 항목이 없습니다.")
                        store.set_meta(META_LAST_PULL_AT, now_iso())
                        raise typer.Exit(EXIT_OK)

                    _section("다운로드")
                    failures = _execute_pull(drive, store, scanner, entries, base, plan, p, log)
                    store.set_meta(META_LAST_PULL_AT, now_iso())
                    if plan.protected:
                        _out("")
                        _err(f"  보호로 건너뛴 항목 {len(plan.protected)}건 — 로컬 내용을 지키기 위해 받지 않았습니다.")
                        _list_reasons("보호", plan.protected)
                    if failures:
                        raise typer.Exit(EXIT_FAIL)


@app.command()
def reconcile(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    dry_run: bool = typer.Option(False, "--dry-run", help="대조만 하고 DB를 바꾸지 않음"),
    trust_size: bool = typer.Option(
        False, "--trust-size",
        help="크기가 같으면 내용도 같다고 보고 원격을 받지 않음(빠르지만 덜 엄밀)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """로컬·원격에 같은 파일이 있는데 기준선이 없는 상태를 해소합니다.

    이 상태에서는 push가 '보류'하고 pull이 '보호'해 양쪽 다 멈춘다 — 어느 쪽이
    최신인지 알 수 없기 때문이다. 원격 사본을 받아 MD5로 대조해서, 내용이 같으면
    기준선만 기록하고(전송 없음) 다르면 충돌로 표시한다.
    **로컬 파일은 어떤 경우에도 건드리지 않는다.**
    """
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        p = _load_profile(profile)
        _require_ready(p)
        _section(f"reconcile (profile={p.name}{', dry-run' if dry_run else ''})")

        root = p.root_path
        if not os.path.isdir(ext_path(root)):
            _fail(f"로컬 루트가 없습니다: {root}", EXIT_CONFIG)

        with _instance_lock(p.name), Store(db_path(p.name)) as store:
            scanner = LocalScanner(root, p.exclude, logger=log)
            _out("  로컬 스캔 중...")
            entries = scanner.scan()
            base = store.all_by_key(p.drive_id)

            targets = []
            # DB 레코드가 없는(또는 file_id를 모르는) 로컬 파일. init 이후 원격에
            # 새로 생긴 파일은 DB에 기록이 없어, 원격을 직접 걸어야 상대를 찾는다.
            # (pull이 '기준선 없음 — reconcile로 대조하세요'라고 안내하는 상태가
            #  정확히 이것인데, 예전에는 DB만 봐서 대조 대상 0건으로 끝났다 —
            #  2026-08-04 사용자 테스트 UT-04에서 발견)
            orphans: dict[str, object] = {}
            for key, entry in entries.items():
                if entry.is_dir:
                    continue
                rec = base.get(key)
                # 대상: 원격에도 있고(file_id) 기준선이 없는(local_md5 없음) 파일
                if rec is not None and rec.file_id and not rec.is_dir and not rec.local_md5:
                    targets.append((key, entry, rec))
                    continue
                if rec is None or (not rec.local_md5 and not rec.file_id
                                   and not rec.is_dir):
                    orphans[key] = entry

            _kv([("로컬 항목", f"{len(entries)}건"),
                 ("대조 대상(DB 기록)", f"{len(targets)}건"),
                 ("DB에 없는 로컬 파일", f"{len(orphans)}건 — 원격에서 상대를 찾습니다")])
            if not targets and not orphans:
                _out("")
                _out("해소할 항목이 없습니다.")
                raise typer.Exit(EXIT_OK)

            same: list[str] = []
            diff: list[tuple[str, str]] = []
            failed: list[tuple[str, str]] = []
            verify_dir = state_dir(p.name) / "verify"

            _section("대조" + (" (크기만)" if trust_size else " (원격 내용 확인)"))
            progress = _Progress("대조", every=10)
            with _drive_api(p, log) as drive:
                if orphans:
                    # 원격 walk로 같은 경로키의 파일을 찾는다. 경로키 충돌(대소문자·
                    # 정규화만 다른 이름)은 pull과 같은 규칙 — 먼저 본 것이 정본이고
                    # 뒤에 온 것은 무시한다.
                    root_id, prefix = _resolve_remote_root(
                        drive, p.drive_id, p.remote_path)
                    seen_rkeys: set[str] = set()
                    matched = 0
                    for rf, full in drive.walk(
                            p.drive_id, root_id,
                            base_path=("/" + prefix) if prefix else ""):
                        rel_r = _rel_from_remote(full, prefix)
                        if not rel_r or rf.is_dir or not rf.id:
                            continue
                        rkey = path_key(rel_r)
                        if rkey in seen_rkeys:
                            continue
                        seen_rkeys.add(rkey)
                        ent = orphans.pop(rkey, None)
                        if ent is None:
                            continue
                        targets.append((rkey, ent, FileRecord(
                            drive_id=p.drive_id, rel_path=ent.rel_path,
                            file_id=rf.id, parent_id=rf.parent_id or "",
                            server_name=rf.name, is_dir=False,
                            remote_size=rf.size, remote_version=rf.version,
                            remote_revision=rf.revision)))
                        matched += 1
                    _kv([("원격에서 상대 확인", f"{matched}건"),
                         ("원격에 없음(push 대상)", f"{len(orphans)}건")])
                    if not targets:
                        _out("")
                        _out("해소할 항목이 없습니다. (원격에 상대가 없는 로컬 파일은 push가 올립니다)")
                        raise typer.Exit(EXIT_OK)
                for key, entry, rec in targets:
                    rel = entry.rel_path
                    try:
                        # 크기가 다르면 내용도 다르다 — 받아볼 필요가 없다.
                        if rec.remote_size is not None and entry.size != rec.remote_size:
                            diff.append((rel, f"크기 다름(로컬 {_human_size(entry.size)} / "
                                              f"원격 {_human_size(rec.remote_size)})"))
                            progress.tick()
                            continue

                        local_md5 = scanner.fill_md5(entry).md5
                        if trust_size:
                            same.append(rel)
                        else:
                            rmd5 = drive.remote_md5(p.drive_id, rec.file_id, verify_dir)
                            if rmd5 and rmd5 == local_md5:
                                same.append(rel)
                            else:
                                diff.append((rel, "내용 다름(MD5 불일치)"))
                                progress.tick()
                                continue

                        if not dry_run:
                            store.upsert_file(FileRecord(
                                drive_id=p.drive_id, rel_path=rel, file_id=rec.file_id,
                                parent_id=rec.parent_id, server_name=rec.server_name,
                                is_dir=False,
                                local_mtime_ns=entry.mtime_ns, local_size=entry.size,
                                local_md5=local_md5,
                                remote_revision=rec.remote_revision,
                                remote_version=rec.remote_version,
                                remote_md5=local_md5, remote_size=rec.remote_size,
                                sync_status="synced", last_synced_at=now_iso(),
                            ))
                    except Exception as exc:
                        failed.append((rel, f"{type(exc).__name__}: {exc}"))
                        log.error("대조 실패 %s: %s", rel, exc)
                    progress.tick()
            progress.done()

            _section("결과")
            _kv([
                ("내용 동일(기준선 기록)", f"{len(same)}건"),
                ("내용 다름(사용자 판단 필요)", f"{len(diff)}건"),
                ("대조 실패", f"{len(failed)}건"),
            ])
            if diff:
                _out("")
                _err("  아래 파일은 로컬과 원격의 내용이 다릅니다. 어느 쪽을 살릴지 정하세요:")
                _err("    로컬을 올리려면  dsync push --assume-local-newer")
                _err("    원격을 받으려면  로컬 파일을 다른 이름으로 옮긴 뒤 dsync pull")
                for rel, why in diff[:_MAX_PLAN_ROWS]:
                    _err(f"      - {rel}: {why}")
            if failed:
                _out("")
                _list_reasons("실패", failed)
            if dry_run:
                _out("")
                _out("dry-run — DB를 바꾸지 않았습니다.")
            raise typer.Exit(EXIT_FAIL if failed else EXIT_OK)


# ---------------------------------------------------------------------------
# sync — 양방향 (M2)
# ---------------------------------------------------------------------------


META_LAST_SYNC_AT = "last_sync_at"

# 되돌리기 어려운 동작을 사용자가 표에서 바로 알아보게 한다.
_KIND_EFFECT = {
    "DOWNLOAD_UPDATE": "로컬 파일을 원격본으로 교체",
    "UPLOAD_VERSION": "원격 파일을 로컬본으로 교체",
    "CONFLICT": "로컬 원본을 충돌 사본으로 개명 후 원격본 수신",
    "LOCAL_TRASH": "로컬을 휴지통으로",
    "REMOTE_TRASH": "원격을 휴지통으로",
    "LOCAL_MOVE": "로컬 파일 이동",
    "REMOTE_MOVE": "원격 파일 이동",
}


def _print_sync_plan(pl, stats: DiffStats, view) -> None:
    _section("계획")
    if not pl.actions:
        _out("  변경 없음")
    else:
        rows = [
            [ACTION_LABEL.get(a.kind, a.kind), a.rel_path,
             "-" if a.is_dir else _human_size(a.size), a.note]
            for a in pl.actions[:_MAX_PLAN_ROWS]
        ]
        _table(["동작", "경로", "크기", "비고"], rows)
        if len(pl.actions) > _MAX_PLAN_ROWS:
            _out(f"  ... 외 {len(pl.actions) - _MAX_PLAN_ROWS}건")

    # 표가 잘려도 무엇이 얼마나 일어나는지는 반드시 보이게 한다. 특히 '덮어쓰기'
    # 계열(갱신받기 / 새버전업로드)과 '충돌 사본 생성', '휴지통'은 되돌리기 어렵다.
    if pl.counts:
        _out("")
        _out("  동작별 건수")
        _table(["동작", "건수", "설명"], [
            [ACTION_LABEL.get(k, k), f"{pl.counts[k]}건", _KIND_EFFECT.get(k, "")]
            for k in sorted(pl.counts, key=lambda x: -pl.counts[x])
        ])

    delete_line = f"{pl.delete_count}건"
    if pl.delete_actions and pl.delete_count != pl.delete_actions:
        delete_line += f" (삭제 동작 {pl.delete_actions}건 — 폴더는 하위 전체가 함께 사라짐)"
    _out("")
    _kv([
        ("올릴 용량", _human_size(pl.bytes_up)),
        ("받을 용량", _human_size(pl.bytes_down)),
        ("실제로 사라질 항목", delete_line),
        ("변경 없음", f"{stats.unchanged}건"),
        ("원격 미확인(판단 보류)", f"{stats.skipped_unobserved}건"),
        ("로컬 미확인(판단 보류)", f"{stats.skipped_local_unobserved}건"),
        ("보고만", f"{len(pl.reports)}건"),
        ("보호", f"{len(pl.protected)}건"),
        ("unsyncable", f"{len(pl.unsyncable)}건"),
        ("다음 실행으로 미룸", f"{len(pl.deferred)}건"),
    ])
    if stats.md5_probe_skipped:
        _err(f"  주의: 원격 내용 대조 예산을 넘겨 {stats.md5_probe_skipped}건을 확인하지 못했습니다"
             " — 그 항목은 충돌로 처리됩니다(--md5-probes 로 늘릴 수 있습니다).")
    if stats.hash_failures:
        _err(f"  읽기 실패 {len(stats.hash_failures)}건:")
        _list_reasons("읽기 실패", stats.hash_failures)
    if view is not None and view.truncated:
        _err("  주의: 원격 변경 목록이 잘렸습니다 — 이번 실행은 부분 처리이며 다음 실행에서 이어집니다.")
    if view is not None and view.probe_skipped:
        _err(f"  주의: 미완료 레코드 {view.probe_skipped}건의 원격 상태를 확인하지 못했습니다"
             " — 'dsync sync --full' 로 한 번 전체 재조정하는 것을 권합니다.")
    _list_reasons("보고", pl.reports)
    _list_reasons("보호", pl.protected)
    _list_reasons("unsyncable", pl.unsyncable)
    _list_reasons("미룸", pl.deferred)


@app.command()
def sync(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    dry_run: bool = typer.Option(False, "--dry-run", help="계획만 출력하고 아무것도 바꾸지 않음"),
    full: bool = typer.Option(False, "--full", help="원격 전체 재조정(목록 API 전체 순회)"),
    propagate_deletes: bool = typer.Option(
        False, "--propagate-deletes",
        help="이번 실행에 한해 삭제를 반대쪽에 전파(설정은 바꾸지 않음)"),
    allow_bulk_delete: bool = typer.Option(
        False, "--allow-bulk-delete", help="대량 삭제 임계를 넘겨 진행"),
    md5_probes: int = typer.Option(
        200, "--md5-probes", help="내용 대조를 위해 원격을 받아 볼 최대 건수"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """양방향 동기화 1회. 충돌은 양쪽을 모두 보존하고, 삭제는 어떤 충돌에서도 이기지 않습니다."""
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        p = _load_profile(profile)
        _require_ready(p)
        do_delete = bool(propagate_deletes or p.propagate_deletes)

        with _instance_lock(p.name):
            mode = "전체" if full else "델타"
            _section(f"sync (profile={p.name}, {mode}{', dry-run' if dry_run else ''})")
            root = p.root_path
            if not os.path.isdir(ext_path(root)):
                _fail(f"로컬 루트가 없습니다: {root}", EXIT_CONFIG)

            with Store(db_path(p.name)) as store:
                # 1) 크래시 복구 — 반드시 잠금 안에서, 다른 무엇보다 먼저
                incomplete = list(store.iter_incomplete())
                # 저널이 비어 있어도 임시파일 찌꺼기는 남을 수 있다(기록 직전에 죽은 경우).
                # 그래서 실제 실행에서는 복구를 **항상** 부른다 — 할 일이 없으면 값이 싸다.
                if incomplete or not dry_run:
                    if dry_run:
                        _section("중단된 작업 복구")
                        _out(f"  미완료 저널 {len(incomplete)}건 — 실제 실행 시 복구합니다(지금은 건드리지 않음)")
                    else:
                        rep = recover(store, p.drive_id, root, logger=log)
                        if rep.scanned or rep.tmp_removed:
                            _section("중단된 작업 복구")
                            _kv([
                                ("복구 표시", f"{rep.scanned}건"),
                                ("임시파일 정리", f"{rep.tmp_removed}건"),
                                ("보존한 충돌 사본", f"{len(rep.conflicts_kept)}건"),
                            ])
                            for rel, status in rep.marked[:10]:
                                _out(f"    재검사 대상: {rel} ({status})")

                # 2) 로컬 스캔
                scanner = LocalScanner(root, p.exclude, logger=log)
                _out("")
                _out("  로컬 스캔 중...")
                entries = scanner.scan()
                base = store.all_by_key(p.drive_id)
                _kv([
                    ("로컬 항목", f"{len(entries)}건"),
                    ("base 레코드", f"{len(base)}건"),
                    ("스캔 제외", f"{len(scanner.skipped)}건"),
                ])
                for s in scanner.skipped[:10]:
                    _err(f"  건너뜀: {s.rel_path} — {s.reason}")

                with _drive_api(p, log) as drive:
                    root_id, prefix = _resolve_remote_root(drive, p.drive_id, p.remote_path)
                    collector = RemoteCollector(drive, p.drive_id, prefix, root_id,
                                                exclude=p.exclude, logger=log)
                    cursor = store.get_cursor()
                    scanned_before = store.get_meta(META_LAST_FULL_SCAN)
                    use_full = bool(full or cursor.revision <= 0 or not scanned_before)

                    _section("원격 상태 " + ("전체 순회" if use_full else "변경 수집"))
                    progress = _Progress("원격 항목", every=100)
                    if use_full:
                        # 순회 중에 일어난 변경을 건너뛰지 않도록 커서를 **먼저** 확보한다.
                        tip = drive.advance_to_tip(p.drive_id, cursor)
                        view = collector.full(on_item=lambda _rel: progress.tick())
                        next_cursor = tip
                    else:
                        view = collector.delta(
                            cursor,
                            known_by_file_id=iter_known_by_file_id(base.values()),
                            dirty_file_ids=store.dirty_file_ids(p.drive_id),
                            on_item=lambda _rel: progress.tick(),
                        )
                        next_cursor = view.cursor
                    progress.done()
                    _kv([
                        ("관측 항목", f"{len(view.entries)}건"),
                        ("원격 삭제", f"{len(view.deleted_keys)}건"),
                        ("범위 밖 이동", f"{len(view.moved_out_keys)}건"),
                        ("changes 항목", f"{view.changes_seen}건"),
                        ("하위 재열람", f"{view.subtrees_relisted}건"),
                        ("커서", f"revision={next_cursor.revision}"),
                    ])
                    for c in view.collisions[:10]:
                        _err(f"  경고: 경로키 충돌로 제외 — {c}")

                    # 3) 3-way diff
                    verify_dir = state_dir(p.name) / "verify"

                    def _probe(r) -> str | None:
                        return drive.remote_md5(p.drive_id, r.file_id, verify_dir) or None

                    decisions, stats = diff(
                        base=base, local=entries, remote=view,
                        propagate_deletes=do_delete,
                        hash_local=scanner.fill_md5,
                        md5_probe=_probe, md5_probe_budget=max(0, md5_probes),
                        # 스캔이 못 읽은 경로는 '없음'이 아니라 '모름'이다 — 삭제 판정 금지.
                        local_unobserved=[s.rel_path for s in scanner.skipped],
                    )

                    # 4) 계획 + 안전 게이트
                    trash_why = trash_unavailable_reason()
                    try:
                        pl = build_plan(decisions, base_count=len(base), p=p,
                                        allow_bulk_delete=allow_bulk_delete,
                                        trash_ok=trash_why is None,
                                        trash_reason=trash_why or "",
                                        # 폴더 삭제가 실제로 몇 건을 지우는지 환산하는 근거
                                        base_keys=tuple(base.keys()))
                    except BulkDeleteAbort as exc:
                        _fail(str(exc), EXIT_FAIL)
                        raise AssertionError("도달 불가")
                    _print_sync_plan(pl, stats, view)

                    if dry_run:
                        _out("")
                        _out("dry-run — 아무것도 변경하지 않았습니다.")
                        raise typer.Exit(EXIT_OK)

                    for rel, reason in stats.hash_failures:
                        _record_error(store, p.drive_id, rel, False, reason)
                    _mark_unsyncable_rows(store, p.drive_id, pl.unsyncable)

                    if not pl.actions:
                        store.set_meta(META_LAST_SYNC_AT, now_iso())
                        _save_cursor(store, next_cursor, use_full)
                        _out("")
                        _out("변경 없음.")
                        raise typer.Exit(EXIT_FAIL if stats.hash_failures else EXIT_OK)

                    # 5) 실행
                    _section("실행")
                    journal = SyncJournal(store)
                    ex = SyncExecutor(drive, store, p, base, journal,
                                      root_id=root_id, remote_prefix=prefix, logger=log,
                                      # 폴더 삭제 직전 하위 전체를 스캔 시점과 대조하는 근거
                                      local_snapshot=entries,
                                      # 스캔이 못 읽은 경로는 실행 단계에서도 삭제 금지
                                      local_unobserved=[s.rel_path for s in scanner.skipped])
                    run_progress = _Progress("처리", every=20)
                    report = ex.run(pl, on_progress=lambda _a: run_progress.tick())
                    run_progress.done()

                    store.set_meta(META_LAST_SYNC_AT, now_iso())
                    # 이번에 원격 상태를 확인한 미완료 레코드에 확인 시각을 남긴다 —
                    # 다음 실행의 dirty 회전이 그 뒤부터 시작한다(앞부분만 반복 확인 방지).
                    if view.probed_ids:
                        store.touch_seen(p.drive_id, sorted(view.probed_ids))
                    # 커서는 실행이 끝난 뒤에만 전진시킨다. 실패한 항목은 sync_status가
                    # 'synced'가 아니므로 다음 실행의 dirty 조회가 반드시 다시 확인한다.
                    _save_cursor(store, next_cursor, use_full)
                    _sync_summary(report, pl)

                    if report.failures or stats.hash_failures:
                        raise typer.Exit(EXIT_FAIL)


def _save_cursor(store: Store, cursor, was_full: bool) -> None:
    store.set_cursor(cursor)
    if was_full:
        store.set_meta(META_LAST_FULL_SCAN, now_iso())


def _mark_unsyncable_rows(store: Store, drive_id: str, rows: list[tuple[str, str]]) -> None:
    for rel, reason in rows:
        rec = store.get_by_path(drive_id, rel) or FileRecord(drive_id=drive_id, rel_path=rel)
        rec.sync_status = "unsyncable"
        rec.error_msg = reason[:500]
        store.upsert_file(rec)


def _sync_summary(report, pl) -> None:
    _section("결과")
    rows = [(ACTION_LABEL.get(k, k), f"{v}건") for k, v in sorted(report.done.items())]
    _kv(rows or [("수행", "0건")])
    _out("")
    _kv([
        ("올림", _human_size(report.bytes_up)),
        ("받음", _human_size(report.bytes_down)),
        ("충돌 사본", f"{len(report.conflicts)}건"),
        ("보호로 건너뜀", f"{len(report.protected)}건"),
        ("실패", f"{len(report.failures)}건"),
    ])
    for rel, copy in report.conflicts[:20]:
        _err(f"  충돌: {rel} — 로컬 사본 보존: {copy}")
    if report.conflicts:
        _err("        'dsync resolve' 로 어느 쪽을 살릴지 정할 수 있습니다.")
    for old, new in report.renamed_by_server[:10]:
        _err(f"  알림: 서버 저장명이 다릅니다 — 로컬 '{old}' → 서버 '{new}' (R14)")
    _list_reasons("보호", report.protected)
    if report.failures:
        _out("")
        _table(["경로", "사유"], [[r, s[:120]] for r, s in report.failures[:_MAX_PLAN_ROWS]])
        if len(report.failures) > _MAX_PLAN_ROWS:
            _out(f"  ... 외 {len(report.failures) - _MAX_PLAN_ROWS}건")


# ---------------------------------------------------------------------------
# resolve — 충돌 해결 (M2)
# ---------------------------------------------------------------------------


@app.command()
def resolve(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    list_only: bool = typer.Option(False, "--list", help="목록만 출력"),
    conflict_id: int = typer.Option(0, "--id", help="해결할 충돌 id(0이면 전체 대화식)"),
    keep: str = typer.Option("", "--keep", help="local | remote | both"),
    dry_run: bool = typer.Option(False, "--dry-run", help="무엇을 할지만 출력"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """미해결 충돌을 확인하고 어느 쪽을 살릴지 정합니다.

    충돌이 생기면 로컬 원본은 '이름 (충돌 날짜 시각).확장자'로 보존되고 원격본이 원래
    경로에 놓입니다. **어느 선택지도 파일을 영구 삭제하지 않습니다**(로컬은 휴지통).
    """
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        p = _load_profile(profile)
        _section(f"resolve (profile={p.name}{', dry-run' if dry_run else ''})")

        with _instance_lock(p.name), Store(db_path(p.name)) as store:
            rows = list(store.iter_unresolved())
            if not rows:
                _out("  미해결 충돌이 없습니다.")
                raise typer.Exit(EXIT_OK)

            _table(
                ["id", "경로", "종류", "보존된 로컬 사본", "시각"],
                [[r["id"], r["rel_path"], r["kind"], r["local_copy_path"] or "-", r["ts"]]
                 for r in rows],
            )
            if list_only:
                _out("")
                _out("  해결: dsync resolve --id <id> --keep local|remote|both")
                raise typer.Exit(EXIT_OK)

            targets = [r for r in rows if not conflict_id or int(r["id"]) == conflict_id]
            if conflict_id and not targets:
                _fail(f"충돌 id {conflict_id} 를 찾을 수 없습니다.", EXIT_FAIL)

            choice = (keep or "").strip().lower()
            if choice and choice not in ("local", "remote", "both"):
                _fail("--keep 은 local, remote, both 중 하나여야 합니다.", EXIT_CONFIG)
            if not choice and not sys.stdin.isatty():
                _fail("대화형 입력이 불가능한 환경입니다. --keep 을 지정하거나 --list 를 쓰세요.",
                      EXIT_CONFIG)

            # local/remote 선택은 원격 사본까지 정리해야 수렴한다 — 그때만 연결한다.
            need_remote = choice in ("local", "remote") or not choice
            drive_cm = _drive_api(p, log) if (need_remote and p.drive_id) else None
            drive = None
            if drive_cm is not None:
                try:
                    drive = drive_cm.__enter__()
                except Exception as exc:  # noqa: BLE001 — 연결 실패해도 로컬 처리는 진행
                    _err(f"  경고: 원격에 연결하지 못해 원격 사본은 그대로 둡니다 — {exc}")
                    drive_cm = None

            done = 0
            for row in targets:
                pick = choice
                if not pick:
                    _out("")
                    _out(f"  충돌 #{row['id']}: {row['rel_path']}")
                    _out(f"    원래 경로  : 원격본 (지금 이 자리에 있음)")
                    _out(f"    로컬 사본  : {row['local_copy_path'] or '-'}")
                    pick = typer.prompt(
                        "    어느 쪽을 살릴까요? [both=둘 다 유지 / local=내 사본을 원래 자리로 / remote=사본 버림]",
                        default="both").strip().lower()
                    if pick not in ("local", "remote", "both"):
                        _err("    건너뜁니다(입력이 올바르지 않음).")
                        continue
                if _resolve_one(store, p, row, pick, dry_run, log, drive=drive):
                    done += 1

            if drive_cm is not None:
                drive_cm.__exit__(None, None, None)

            _out("")
            _out(f"{'(dry-run) ' if dry_run else ''}처리한 충돌: {done}건")


def _drop_remote_copy(store: Store, p: Profile, copy_rel: str, drive, dry_run: bool) -> None:
    """충돌 사본의 **원격본**까지 휴지통으로 보낸다(휴지통이므로 복구 가능).

    사본은 기본 설정에서 원격에도 올라간다(`upload_conflict_copy`). 로컬만 정리하고
    원격을 남기면 **다음 sync가 그 사본을 다시 받아와** 사용자가 방금 해결한 상태로
    되돌아간다(실계정에서 확인). 'local'/'remote' 선택은 "이 사본은 더 필요 없다"는
    뜻이므로 양쪽에서 정리해야 해결이 수렴한다. 'both'는 아무것도 지우지 않는다.
    """
    if not copy_rel:
        return
    rec = store.get_by_path(p.drive_id, copy_rel)
    if rec is None:
        return
    if rec.file_id and drive is not None:
        _out(f"    → 원격 사본도 휴지통으로 보냅니다: {copy_rel}")
        if not dry_run:
            try:
                drive.move_to_trash(p.drive_id, rec.file_id)
            except DoorayApiError as exc:
                if exc.result_code != NO_ACCESS_AUTHORITY:
                    _err(f"    경고: 원격 사본 정리 실패(로컬은 처리됨) — {exc}")
    elif rec.file_id:
        _err(f"    알림: 원격 사본이 남아 있습니다 — 다음 sync가 다시 받아옵니다: {copy_rel}")
    if not dry_run:
        store.delete_by_key(p.drive_id, path_key(copy_rel))


def _resolve_one(store: Store, p: Profile, row: dict, pick: str, dry_run: bool, log,
                 drive=None) -> bool:
    """충돌 1건 처리. 어떤 선택지에서도 파일을 영구 삭제하지 않는다(휴지통만)."""
    rel = str(row["rel_path"])
    copy_path = row["local_copy_path"]
    cid = int(row["id"])

    if pick == "both":
        _out(f"    → 둘 다 유지하고 해결 처리합니다: {rel}")
        if not dry_run:
            store.resolve_conflict(cid)
        return True

    if not copy_path or not os.path.exists(ext_path(copy_path)):
        _err(f"    사본을 찾을 수 없어 '둘 다 유지'로 처리합니다: {copy_path or '-'}")
        if not dry_run:
            store.resolve_conflict(cid)
        return True

    copy_rel = _rel_of(p, copy_path)

    if pick == "remote":
        _out(f"    → 로컬 사본을 휴지통으로 보냅니다: {copy_path}")
        _drop_remote_copy(store, p, copy_rel, drive, dry_run)
        if not dry_run:
            send_to_trash(copy_path)
            store.resolve_conflict(cid)
        return True

    # local: 사본을 원래 자리로 되돌리고, 원래 자리에 있던 원격본은 휴지통으로 보낸다.
    dest = local_path(p.root_path, rel)
    _out(f"    → 사본을 원래 경로로 되돌립니다: {copy_path} → {dest}")
    # 사본을 원래 경로로 되돌리므로 그 내용은 원래 경로에서 이어진다 — 사본 자체는
    # 더 이상 필요 없다. 원격에 남겨 두면 다음 sync가 다시 받아와 해결이 되돌아간다.
    _drop_remote_copy(store, p, copy_rel, drive, dry_run)
    if dry_run:
        return True
    if os.path.exists(ext_path(dest)):
        send_to_trash(dest)          # 원격본도 지우지 않고 휴지통으로
    os.replace(ext_path(copy_path), ext_path(dest))
    rec = store.get_by_path(p.drive_id, rel) or FileRecord(drive_id=p.drive_id, rel_path=rel)
    # 기준선(local_md5)은 '마지막으로 원격과 일치했던 내용'의 해시다 — 지금 원래 자리에
    # 있던 **원격본**의 해시이며, download()가 넣어 둔 값이다. 이것을 복원한 사본의
    # 해시로 덮어쓰면 다음 diff가 '로컬 = base'로 보아 **아무 일도 하지 않는다.**
    # (기준선을 NULL로 지우면 영구 PROTECT, 새 내용으로 덮으면 영구 무동작 — 둘 다 틀렸다.)
    # 기준선은 그대로 두고 (mtime, size)만 비워 다음 diff가 반드시 해시를 다시 계산하게 한다.
    baseline = rec.local_md5 or rec.remote_md5 or None
    rec.local_mtime_ns = None
    rec.local_size = None
    rec.local_md5 = baseline
    rec.sync_status = "pending_upload"
    rec.error_msg = "충돌 해결(로컬 우선) — 다음 sync에서 업로드"
    if not baseline:
        # 기준선을 모르면 어느 쪽이 최신인지 판단할 수 없다. 조용히 넘어가지 말고
        # 다음 sync가 '보호'로 드러내게 둔다(무성 실패보다 눈에 보이는 편이 낫다).
        rec.sync_status = "error"
        rec.error_msg = "충돌 해결(로컬 우선) — 기준선 없음, 다음 sync가 보류합니다"
        _err("    경고: 기준선이 없어 다음 sync가 이 파일을 보류합니다"
             " — 'dsync reconcile'로 대조하세요.")
    store.upsert_file(rec)
    store.resolve_conflict(cid)          # 사본 레코드는 _drop_remote_copy가 이미 정리했다
    return True


def _rel_of(p: Profile, abs_path: str) -> str:
    """동기화 루트 기준 상대경로. 루트 밖이면 빈 문자열."""
    try:
        return rel_posix(p.root_path, Path(abs_path))
    except ValueError:
        return ""


def _print_pull_plan(plan: _PullPlan) -> None:
    _section("계획")
    if not plan.items:
        _out("  변경 없음")
    else:
        rows = [
            [_PULL_LABEL.get(i.op, i.op), i.rel,
             "-" if i.op == "MKDIR" else _human_size(i.size), i.note]
            for i in plan.items[:_MAX_PLAN_ROWS]
        ]
        _table(["동작", "경로", "크기", "비고"], rows)
        if len(plan.items) > _MAX_PLAN_ROWS:
            _out(f"  ... 외 {len(plan.items) - _MAX_PLAN_ROWS}건")
    _out("")
    _kv([
        ("이미 최신", f"{plan.up_to_date}건"),
        ("보호(로컬 우선)", f"{len(plan.protected)}건 — 덮어쓰지 않습니다"),
        ("unsyncable", f"{len(plan.unsyncable)}건 — Windows에 저장할 수 없는 이름"),
        ("원격에서 사라짐", f"{plan.remote_missing}건 — M1은 로컬을 삭제하지 않습니다(보고만)"),
    ])
    _list_reasons("보호", plan.protected)
    _list_reasons("unsyncable", plan.unsyncable)


def _list_reasons(label: str, rows: list[tuple[str, str]], limit: int = 20) -> None:
    for rel, why in rows[:limit]:
        _err(f"  {label}: {rel} — {why}")
    if len(rows) > limit:
        _err(f"  {label}: ... 외 {len(rows) - limit}건 (자세한 내역은 로그 파일 참조)")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _check(ok: bool | None, title: str, detail: object = "") -> bool:
    marker = {True: "[정상]", False: "[실패]", None: "[경고]"}[ok]
    _out(f"  {marker} {title}" + (f" — {detail}" if detail != "" else ""))
    return ok is not False


def _doctor_long_path(root: Path) -> tuple[bool | None, str]:
    r"""\\?\ 접두로 260자 초과 경로를 실제로 만들고 읽고 지운다(C3)."""
    stem = Path(root) / ".dooraysync_tmp" / f"doctor_{uuid.uuid4().hex[:8]}"
    deep = stem
    for _ in range(8):
        deep = deep / ("d" * 40)
    target = deep / "긴경로테스트.txt"
    created: list[Path] = []
    try:
        os.makedirs(ext_path(deep), exist_ok=True)
        node = deep
        while node != stem.parent:
            created.append(node)
            node = node.parent
        with open(ext_path(target), "w", encoding="utf-8") as f:
            f.write("ok")
        with open(ext_path(target), "r", encoding="utf-8") as f:
            if f.read() != "ok":
                return False, "쓰기/읽기 내용 불일치"
        return True, f"{len(str(target))}자 경로 생성·읽기 성공"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            os.remove(ext_path(target))
        except OSError:
            pass
        for node in created:      # 깊은 쪽부터 비어 있을 때만 정리
            try:
                os.rmdir(ext_path(node))
            except OSError:
                pass


def _doctor_db(path: Path) -> tuple[bool | None, str]:
    """PRAGMA integrity_check. Store를 열면 스키마를 만들어 버리므로 별도 연결로 읽는다."""
    if not os.path.exists(ext_path(path)):
        return None, f"DB 없음 ({path}) — 'dsync init' 필요"
    conn = None
    try:
        conn = sqlite3.connect(ext_path(path))
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result = ", ".join(str(r[0]) for r in rows) if rows else "(응답 없음)"
        if result.lower() != "ok":
            return False, result[:200]
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return True, f"integrity_check=ok, files {n}건"
    except sqlite3.Error as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()


@app.command()
def doctor(
    profile: str = typer.Option("default", "--profile", "-p", help="프로파일 이름"),
    dry_run: bool = typer.Option(False, "--dry-run", help="(doctor는 진단 전용 — 동작 동일)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="상세 로그"),
) -> None:
    """토큰/연결/rate-limit 헤더/긴 경로/DB 무결성/설정 유효성 점검."""
    log = setup_logging(profile, verbose=verbose)
    with _error_boundary(log):
        _section(f"doctor (profile={profile})")
        ok = True
        config_problem = False

        # 1) 토큰
        _out("")
        _out("1. 토큰")
        token = ""
        try:
            token = get_token()
            source = "환경변수 DOORAY_API_TOKEN" if os.environ.get("DOORAY_API_TOKEN", "").strip() else "keyring"
            ok &= _check(True, f"토큰 확인 ({source})", mask(token))
        except TokenNotFound as exc:
            config_problem = True
            ok &= _check(False, "토큰 없음")
            for line in str(exc).splitlines()[1:]:
                _out(f"      {line}")

        # 2) 설정
        _out("")
        _out("2. 설정")
        p: Profile | None = None
        if not config_exists(profile):
            config_problem = True
            ok &= _check(False, "설정 없음", f"{config_path()} — 'dsync init' 필요")
        else:
            try:
                p = load_config(profile)
            except (FileNotFoundError, ValueError) as exc:
                config_problem = True
                ok &= _check(False, "설정 읽기 실패", exc)
        if p is not None:
            _check(True, "설정 파일", config_path())
            # 값이 비었으면 '실패'가 아니라 '설정 문제'다 — 종료코드 2로 보내야
            # 사용자가 init을 다시 돌려야 한다는 걸 스크립트로도 구분할 수 있다.
            ok &= _check(bool(p.drive_id), "drive_id", p.drive_id or "(미설정)")
            root_ok = bool(p.local_root) and os.path.isdir(ext_path(p.root_path))
            ok &= _check(root_ok, "local_root", p.local_root or "(미설정)")
            config_problem = config_problem or not p.drive_id or not root_ok
            _check(p.base_url.startswith("https://") or None, "base_url", p.base_url)

        # 3) API 연결 + rate-limit
        _out("")
        _out("3. API 연결")
        if not token or p is None:
            _check(None, "연결 점검 생략", "토큰 또는 설정이 없습니다")
        else:
            try:
                with DoorayClient(p.base_url, token, logger=log) as client:
                    drive = DriveAPI(client)
                    drives = _collect_drives(drive)
                    ok &= _check(True, "드라이브 목록 조회", f"{len(drives)}개")
                    if p.drive_id:
                        try:
                            info = drive.get_drive(p.drive_id)
                            ok &= _check(True, "대상 드라이브 접근",
                                         info.get("name") or p.drive_id)
                        except DoorayApiError as exc:
                            ok &= _check(False, "대상 드라이브 접근", exc)
                    rl = client.last_rate_limit
                    _check(
                        True, "rate-limit 헤더",
                        f"remaining={rl.get('remaining')} burst={rl.get('burst')} "
                        f"replenish={rl.get('replenish')}/s requested={rl.get('requested')}",
                    )
            except DoorayApiError as exc:
                ok &= _check(False, "API 호출 실패", exc)
            except Exception as exc:  # 네트워크·TLS
                ok &= _check(False, "연결 실패", f"{type(exc).__name__}: {exc}")

        # 4) 긴 경로 (\\?\)
        _out("")
        _out("4. 긴 경로 지원 (\\\\?\\)")
        target_root = p.root_path if (p is not None and p.local_root
                                      and os.path.isdir(ext_path(p.root_path))) else state_dir(profile)
        os.makedirs(ext_path(target_root), exist_ok=True)
        lp_ok, lp_msg = _doctor_long_path(Path(target_root))
        ok &= _check(lp_ok, f"260자 초과 경로 ({target_root})", lp_msg)

        # 5) DB 무결성
        _out("")
        _out("5. 상태 DB")
        db_ok, db_msg = _doctor_db(db_path(profile))
        ok &= _check(db_ok, str(db_path(profile)), db_msg)

        # 6) 휴지통 — 삭제 전파(M2)의 전제. 없으면 삭제는 '보고'로만 처리된다.
        #    '실패'가 아니라 '경고'다: push/pull은 삭제를 하지 않으므로 이것 없이도 동작한다.
        _out("")
        _out("6. 휴지통(send2trash)")
        trash_why = trash_unavailable_reason()
        _check(
            True if trash_why is None else None,
            "로컬 삭제 → 휴지통",
            "사용 가능" if trash_why is None
            else f"{trash_why} — 'pip install send2trash' 전까지 삭제 전파는 보고만 합니다",
        )

        _out("")
        if config_problem:
            _fail("설정 또는 토큰 문제가 있습니다. 위 안내를 따라 조치하세요.", EXIT_CONFIG)
        if not ok:
            _fail("일부 점검이 실패했습니다.", EXIT_FAIL)
        _out("모든 점검 통과.")


# ---------------------------------------------------------------------------
# 앱 콜백
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        _out(f"dsync {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="버전 출력 후 종료",
    ),
) -> None:
    """Dooray Drive 로컬 동기화 CLI."""


if __name__ == "__main__":
    app()
