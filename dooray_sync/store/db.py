"""SQLite 상태 저장소 — files / journal / conflicts / meta (모듈규약 §9).

`files`는 3-way diff의 base(마지막 동기화 시점 상태)이고, `journal`은 크래시 복구 근거다.
executor는 "저널 committed 기록"과 "files 갱신"을 **하나의 트랜잭션**으로 묶어야 하므로
(구현계획서 §3.1 core/journal.py), 모든 쓰기 메서드는 `transaction()`을 경유하며
중첩 호출은 SAVEPOINT로 바깥 트랜잭션에 합류한다.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..api.models import Cursor
from ..util.paths import ext_path, path_key, to_nfc

SYNC_STATUSES = ('synced', 'pending_upload', 'pending_download', 'conflict',
                 'unsyncable', 'error', 'ignored')
JOURNAL_PHASES = ('planned', 'started', 'transferred', 'verified', 'committed', 'failed')

# 저널 항목이 "완료"로 간주되는 phase. 이외의 마지막 phase는 복구 대상이다.
TERMINAL_PHASES = ('committed', 'failed')

# meta 테이블 키 (구현계획서 §3.2)
META_CURSOR_REVISION = "revision_cursor"
META_CURSOR_FILE_ID = "cursor_file_id"
META_LAST_FULL_SCAN = "last_full_scan_at"
META_HASH_ALGO = "hash_algo"

# journal.detail 안에 심는 예약 키 — phase 행이 어느 begin 항목에 속하는지 역참조용.
# 스키마에 링크 컬럼을 추가할 수 없어(규약 §9 DDL 고정) detail JSON에 보관한다.
_ENTRY_KEY = "_entry"

_SCHEMA_FILE = "schema.sql"
_ITER_CHUNK = 1000

_FILE_COLUMNS = (
    "drive_id", "file_id", "parent_id", "rel_path", "rel_path_key", "server_name",
    "is_dir", "local_mtime_ns", "local_size", "local_md5", "remote_revision",
    "remote_version", "remote_md5", "remote_size", "sync_status", "last_synced_at",
    "error_msg",
)
_SELECT_FILE = "SELECT id, " + ", ".join(_FILE_COLUMNS) + " FROM files"


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _dumps(detail: dict | None) -> str | None:
    """detail은 항상 JSON 또는 NULL — iter_incomplete의 json_extract가 이를 전제한다."""
    if detail is None:
        return None
    return json.dumps(detail, ensure_ascii=False, default=str)


def _loads(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _read_schema() -> str:
    try:
        from importlib import resources

        return resources.files(__package__).joinpath(_SCHEMA_FILE).read_text(encoding="utf-8")
    except Exception:
        # zip 등 비파일 배포가 아닌 일반 설치/개발 트리 폴백
        return (Path(__file__).resolve().parent / _SCHEMA_FILE).read_text(encoding="utf-8")


@dataclass
class FileRecord:
    drive_id: str
    rel_path: str
    rel_path_key: str = ""
    id: int | None = None
    file_id: str | None = None
    parent_id: str | None = None
    server_name: str | None = None
    is_dir: bool = False
    local_mtime_ns: int | None = None
    local_size: int | None = None
    local_md5: str | None = None
    remote_revision: int | None = None
    remote_version: int | None = None
    remote_md5: str | None = None
    remote_size: int | None = None
    sync_status: str = "synced"
    last_synced_at: str | None = None
    error_msg: str | None = None

    def __post_init__(self) -> None:
        # rel_path와 key의 정규화 형태가 어긋나면 UNIQUE 키가 중복 행을 만든다.
        # 둘 다 여기서 한 번에 확정한다.
        self.rel_path = to_nfc(self.rel_path)
        if not self.rel_path_key:
            self.rel_path_key = path_key(self.rel_path)


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        drive_id=row["drive_id"],
        file_id=row["file_id"],
        parent_id=row["parent_id"],
        rel_path=row["rel_path"],
        rel_path_key=row["rel_path_key"],
        server_name=row["server_name"],
        is_dir=bool(row["is_dir"]),
        local_mtime_ns=row["local_mtime_ns"],
        local_size=row["local_size"],
        local_md5=row["local_md5"],
        remote_revision=row["remote_revision"],
        remote_version=row["remote_version"],
        remote_md5=row["remote_md5"],
        remote_size=row["remote_size"],
        sync_status=row["sync_status"],
        last_synced_at=row["last_synced_at"],
        error_msg=row["error_msg"],
    )


class Store:
    """상태 DB 핸들. 하나의 sqlite3 연결을 락으로 직렬화해 공유한다.

    watch(폴링 스레드) 대비로 `check_same_thread=False`를 쓰되, 연결을 스레드 간
    무보호 공유하면 커서가 섞이므로 모든 접근을 RLock으로 감싼다.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._depth = 0  # transaction() 중첩 깊이
        parent = self.db_path.parent
        if str(parent):
            # 규약 §0.2: 로컬 FS 접근은 ext_path 경유
            Path(ext_path(parent)).mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            ext_path(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # 트랜잭션 경계를 이 클래스가 직접 통제
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL이라도 쓰기끼리는 배타적 — CLI와 데몬이 겹칠 때 즉시 SQLITE_BUSY로 죽지 않게.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self.init_schema()

    # ---------- 수명주기 ----------
    def init_schema(self) -> None:
        """스키마 생성(멱등). schema.sql은 패키지 데이터로 읽는다."""
        with self._lock:
            self._conn.executescript(_read_schema())

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- 트랜잭션 ----------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """예외 시 롤백. 중첩되면 SAVEPOINT로 바깥 트랜잭션에 합류한다 —
        journal 'committed'와 files 갱신을 한 커밋으로 묶기 위한 전제(M2)."""
        with self._lock:
            if self._depth == 0:
                # 나중에 쓰기로 승격하다 SQLITE_BUSY로 데드락 나는 것을 피해 처음부터 IMMEDIATE
                self._conn.execute("BEGIN IMMEDIATE")
                self._depth = 1
                try:
                    yield self._conn
                except BaseException:
                    self._depth = 0
                    self._conn.rollback()
                    raise
                self._depth = 0
                self._conn.commit()
            else:
                self._depth += 1
                sp = f"dsync_sp{self._depth}"
                self._conn.execute(f"SAVEPOINT {sp}")
                try:
                    yield self._conn
                except BaseException:
                    self._depth -= 1
                    try:
                        self._conn.execute(f"ROLLBACK TO {sp}")
                        self._conn.execute(f"RELEASE {sp}")
                    except sqlite3.OperationalError:
                        # 오류로 트랜잭션 전체가 이미 자동 롤백된 경우 — 원래 예외를 우선한다.
                        pass
                    raise
                self._depth -= 1
                self._conn.execute(f"RELEASE {sp}")

    # ---------- meta ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_cursor(self) -> Cursor:
        raw_rev = self.get_meta(META_CURSOR_REVISION)
        try:
            revision = int(raw_rev) if raw_rev else 0
        except ValueError:
            revision = 0
        file_id = self.get_meta(META_CURSOR_FILE_ID) or None
        return Cursor(revision=revision, file_id=file_id)

    def set_cursor(self, cursor: Cursor) -> None:
        # 두 키가 따로 커밋되면 (revision, fileId) 복합 커서가 찢어진다(B2) — 한 트랜잭션으로.
        with self.transaction():
            self.set_meta(META_CURSOR_REVISION, str(cursor.revision))
            self.set_meta(META_CURSOR_FILE_ID, cursor.file_id or "")

    # ---------- files ----------
    def upsert_file(self, rec: FileRecord) -> None:
        """(drive_id, rel_path_key) UNIQUE 기준 삽입/갱신. rec은 전체 상태를 뜻하므로
        None 필드도 그대로 반영한다(부분 갱신 아님)."""
        if rec.sync_status not in SYNC_STATUSES:
            raise ValueError(f"알 수 없는 sync_status: {rec.sync_status!r}")
        if not rec.rel_path_key:
            rec.rel_path_key = path_key(rec.rel_path)

        values = (
            rec.drive_id, rec.file_id, rec.parent_id, rec.rel_path, rec.rel_path_key,
            rec.server_name, int(bool(rec.is_dir)), rec.local_mtime_ns, rec.local_size,
            rec.local_md5, rec.remote_revision, rec.remote_version, rec.remote_md5,
            rec.remote_size, rec.sync_status, rec.last_synced_at, rec.error_msg,
        )
        updatable = [c for c in _FILE_COLUMNS if c not in ("drive_id", "rel_path_key")]
        sql = (
            "INSERT INTO files (" + ", ".join(_FILE_COLUMNS) + ") "
            "VALUES (" + ", ".join("?" * len(_FILE_COLUMNS)) + ") "
            "ON CONFLICT(drive_id, rel_path_key) DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in updatable)
        )
        with self.transaction() as conn:
            conn.execute(sql, values)
            row = conn.execute(
                "SELECT id FROM files WHERE drive_id = ? AND rel_path_key = ?",
                (rec.drive_id, rec.rel_path_key),
            ).fetchone()
        if row is not None:
            rec.id = row["id"]

    def get_by_path(self, drive_id: str, rel_path: str) -> FileRecord | None:
        key = path_key(rel_path)
        with self._lock:
            row = self._conn.execute(
                _SELECT_FILE + " WHERE drive_id = ? AND rel_path_key = ?", (drive_id, key)
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def get_by_file_id(self, file_id: str) -> FileRecord | None:
        with self._lock:
            row = self._conn.execute(
                _SELECT_FILE + " WHERE file_id = ? ORDER BY id LIMIT 1", (file_id,)
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def iter_files(self, drive_id: str) -> Iterator[FileRecord]:
        """id 키셋 페이징으로 청크 단위 스트리밍 — 호출측이 순회 도중 같은 연결로
        upsert/delete를 해도 행이 누락되거나 중복되지 않는다."""
        last_id = 0
        while True:
            with self._lock:
                rows = self._conn.execute(
                    _SELECT_FILE + " WHERE drive_id = ? AND id > ? ORDER BY id LIMIT ?",
                    (drive_id, last_id, _ITER_CHUNK),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield _row_to_record(row)
            last_id = rows[-1]["id"]

    def all_by_key(self, drive_id: str) -> dict[str, FileRecord]:
        return {rec.rel_path_key: rec for rec in self.iter_files(drive_id)}

    def delete_by_key(self, drive_id: str, rel_path_key: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM files WHERE drive_id = ? AND rel_path_key = ?",
                (drive_id, rel_path_key),
            )

    def count_files(self, drive_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM files WHERE drive_id = ?", (drive_id,)
            ).fetchone()
        return int(row["n"])

    def count_by_status(self, drive_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sync_status, COUNT(*) AS n FROM files WHERE drive_id = ? "
                "GROUP BY sync_status ORDER BY sync_status",
                (drive_id,),
            ).fetchall()
        return {row["sync_status"]: int(row["n"]) for row in rows}

    # ---------- journal ----------
    def journal_begin(self, session: str, op: str, *, file_id: str | None = None,
                      rel_path: str | None = None, detail: dict | None = None) -> int:
        """'planned' 행을 남기고 entry_id 반환. 이후 진행은 journal_phase로 덧붙인다."""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO journal (ts, session, op, phase, file_id, rel_path, detail) "
                "VALUES (?, ?, ?, 'planned', ?, ?, ?)",
                (now_iso(), session, op, file_id, rel_path, _dumps(detail)),
            )
            return int(cur.lastrowid)

    def journal_phase(self, entry_id: int, phase: str, detail: dict | None = None) -> None:
        """phase 전이를 **추가 행으로** 기록한다(덮어쓰지 않음) — 크래시 시점의
        진행 이력 자체가 복구 판단 근거이므로 이전 phase를 지우면 안 된다."""
        if phase not in JOURNAL_PHASES:
            raise ValueError(f"알 수 없는 journal phase: {phase!r}")
        with self.transaction() as conn:
            origin = conn.execute(
                "SELECT session, op, file_id, rel_path, "
                f"COALESCE(json_extract(detail, '$.{_ENTRY_KEY}'), id) AS entry "
                "FROM journal WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if origin is None:
                raise KeyError(f"journal entry 없음: {entry_id}")
            payload = dict(detail or {})
            payload[_ENTRY_KEY] = int(origin["entry"])
            conn.execute(
                "INSERT INTO journal (ts, session, op, phase, file_id, rel_path, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), origin["session"], origin["op"], phase,
                 origin["file_id"], origin["rel_path"], _dumps(payload)),
            )

    def iter_incomplete(self) -> Iterator[dict]:
        """항목별 **마지막** phase가 committed/failed가 아닌 저널 항목.

        항목 = journal_begin이 만든 행 + 그에 딸린 phase 행들. 마지막 phase는
        항목 내 최대 id 행이다(추가 순서 = 진행 순서).
        """
        sql = (
            "WITH ent AS ("
            "  SELECT id, ts, session, op, phase, file_id, rel_path, detail,"
            f"         COALESCE(json_extract(detail, '$.{_ENTRY_KEY}'), id) AS entry_id"
            "    FROM journal"
            "), tip AS ("
            "  SELECT entry_id, MAX(id) AS last_id FROM ent GROUP BY entry_id"
            ") "
            "SELECT e.* FROM ent e JOIN tip t ON e.id = t.last_id "
            "WHERE e.phase NOT IN (" + ", ".join("?" * len(TERMINAL_PHASES)) + ") "
            "ORDER BY e.entry_id"
        )
        with self._lock:
            rows = self._conn.execute(sql, TERMINAL_PHASES).fetchall()
        for row in rows:
            detail = _loads(row["detail"])
            detail.pop(_ENTRY_KEY, None)
            yield {
                "entry_id": int(row["entry_id"]),
                "id": row["id"],
                "ts": row["ts"],
                "session": row["session"],
                "op": row["op"],
                "phase": row["phase"],
                "file_id": row["file_id"],
                "rel_path": row["rel_path"],
                "detail": detail,
            }

    # ---------- conflicts ----------
    def add_conflict(self, rel_path: str, kind: str,
                     local_copy_path: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO conflicts (ts, rel_path, kind, local_copy_path, resolved) "
                "VALUES (?, ?, ?, ?, 0)",
                (now_iso(), to_nfc(rel_path), kind, local_copy_path),
            )

    def iter_unresolved(self) -> Iterator[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, rel_path, kind, local_copy_path, resolved "
                "FROM conflicts WHERE resolved = 0 ORDER BY id"
            ).fetchall()
        for row in rows:
            yield dict(row)
