-- dooray_sync 상태 DB 스키마 (모듈규약 §9 / 구현계획서 §3.2)
-- files = 3-way diff의 base(마지막 동기화 시점), journal = 크래시 복구 근거,
-- conflicts = 충돌 사본 기록, meta = changes 커서 등 스칼라 상태.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS files (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  drive_id        TEXT NOT NULL,
  file_id         TEXT,
  parent_id       TEXT,
  rel_path        TEXT NOT NULL,
  rel_path_key    TEXT NOT NULL,
  server_name     TEXT,
  is_dir          INTEGER NOT NULL DEFAULT 0,
  local_mtime_ns  INTEGER,
  local_size      INTEGER,
  local_md5       TEXT,
  remote_revision INTEGER,
  remote_version  INTEGER,
  remote_md5      TEXT,
  remote_size     INTEGER,
  sync_status     TEXT NOT NULL DEFAULT 'synced',
  last_synced_at  TEXT,
  error_msg       TEXT,
  UNIQUE (drive_id, rel_path_key)
);
CREATE INDEX IF NOT EXISTS idx_files_file_id ON files(file_id);
CREATE INDEX IF NOT EXISTS idx_files_status  ON files(sync_status);

CREATE TABLE IF NOT EXISTS journal (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  session  TEXT NOT NULL,
  op       TEXT NOT NULL,
  phase    TEXT NOT NULL,
  file_id  TEXT,
  rel_path TEXT,
  detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_session ON journal(session);

CREATE TABLE IF NOT EXISTS conflicts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  rel_path        TEXT NOT NULL,
  kind            TEXT NOT NULL,
  local_copy_path TEXT,
  resolved        INTEGER NOT NULL DEFAULT 0
);
