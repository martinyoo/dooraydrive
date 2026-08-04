"""M1 회귀 테스트 — 재발하면 조용히 데이터를 잃는 결함들.

각 테스트는 검증에서 실제로 확정된 결함 1건에 대응한다. 이름 뒤 괄호가 근거다.
실행: python -m pytest tests -q   (pytest 미설치 시 python tests/test_regressions.py)
"""
from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dooray_sync.api.models import ChangeItem, Cursor, RemoteFile  # noqa: E402
from dooray_sync.core.scanner import LocalScanner  # noqa: E402
from dooray_sync.store.db import FileRecord, Store  # noqa: E402
from dooray_sync.util.paths import (  # noqa: E402
    ext_path, matches_any, name_issue, path_key, server_name_will_differ, to_nfc,
)


# ---------------------------------------------------------------- 경로/이름
def test_ext_path_preserves_trailing_dot():
    """\\?\\ 접두를 붙이기 전에 경로가 정규화되면 끝 점이 잘린다(PoC-08 실측)."""
    p = ext_path(r"C:\temp\name.")
    assert p.endswith("name."), p
    assert p.startswith("\\\\?\\"), p


def test_ext_path_is_idempotent():
    once = ext_path(r"C:\temp\a.txt")
    assert ext_path(once) == once


def test_path_key_collapses_nfd_and_case():
    nfc = unicodedata.normalize("NFC", "보고서.TXT")
    nfd = unicodedata.normalize("NFD", "보고서.txt")
    assert path_key(nfc) == path_key(nfd)


def test_matches_any_matches_nested_component():
    """'*.tmp'는 하위 경로의 파일도 걸러야 한다."""
    assert matches_any("a/b/c.tmp", ["*.tmp"])
    assert matches_any(".dooraysync/x", [".dooraysync/"])
    assert not matches_any("a/b/c.txt", ["*.tmp"])


def test_server_name_will_differ_detects_space_trim():
    """R14: 서버가 앞뒤 공백을 절삭한다 — 정본을 서버 저장명으로 써야 하는 근거."""
    assert server_name_will_differ(" 앞공백.txt")
    assert server_name_will_differ("뒤공백.txt ")
    assert server_name_will_differ('따옴표".txt')
    assert not server_name_will_differ("정상.txt")


def test_name_issue_flags_windows_hostile_names():
    assert name_issue("CON.txt")
    assert name_issue("끝점.")
    assert name_issue("a<b.txt")
    assert name_issue("정상.txt") is None


# ---------------------------------------------------------------- 모델 파싱
def test_change_item_deleted_has_only_ids():
    """실측: deleted 항목은 name/path/version/hash가 전부 null."""
    ci = ChangeItem.from_api({
        "changeType": "deleted", "revision": "17521",
        "file": {"id": "4387846799304408510", "type": "file", "revision": "17521",
                 "version": None, "name": None, "hash": None, "path": None, "size": None},
    })
    assert ci.is_deleted and ci.revision == 17521 and ci.file_id
    assert ci.name is None and ci.full_path is None


def test_change_item_rename_keeps_version():
    """R13: 이름 변경은 version을 올리지 않는다 → version만 비교하면 개명을 놓친다."""
    before = ChangeItem.from_api({"changeType": "updated", "revision": "17516", "file": {
        "id": "1", "type": "file", "version": 1, "name": "poc05_A.bin",
        "path": "/_poc_sandbox", "hash": "71435221f0dd7f8e0ca561f931315aa2", "size": 81920}})
    after = ChangeItem.from_api({"changeType": "updated", "revision": "17517", "file": {
        "id": "1", "type": "file", "version": 1, "name": "poc05_A_renamed.bin",
        "path": "/_poc_sandbox", "hash": "71435221f0dd7f8e0ca561f931315aa2", "size": 81920}})
    assert before.version == after.version
    assert before.full_path != after.full_path   # 이름/경로로만 감지 가능


def test_cursor_uses_latest_revision_param():
    """실측: 실제 필터 파라미터는 latestRevision (revision= 은 무시됨)."""
    params = Cursor(revision=17514, file_id="abc").as_params()
    assert params.get("latestRevision") == 17514
    assert "revision" not in params


def test_cursor_never_sends_file_id():
    """**2026-08-03 정정.** 초판 규약은 (revision, fileId) 복합 커서가 필수라고 했으나,
    실계정 대조 실험에서 fileId를 실으면 넣지 않았을 때 반환되는 항목이 누락됐다:

        latestRevision=23776 + fileId=<a.txt>   → 0건
        latestRevision=23776                    → 1건 (rev=23778 원격.txt)

    커서에 fileId가 박히는 순간 이후 원격 변경을 영구히 놓친다. M1은 changes를
    소비하지 않아 드러나지 않았고 M2 델타 모드에서 처음 터졌다.
    """
    params = Cursor(revision=23776, file_id="4390769620640107440").as_params()
    assert "fileId" not in params, "fileId를 실으면 원격 변경을 영구 누락한다"
    assert params == {"latestRevision": 23776}


def test_remote_file_ignores_unknown_fields():
    rf = RemoteFile.from_api({"id": "1", "name": "a.txt", "type": "file",
                              "미지필드": "무시", "size": 10})
    assert rf.id == "1" and rf.size == 10


# ---------------------------------------------------------------- changes 페이징
class _FakeChangesClient:
    """부분 페이지를 돌려주는 서버 흉내 — size보다 적게 주면서 뒤에 데이터가 남아 있다."""

    def __init__(self, total: int, chunk: int) -> None:
        self.items = [
            {"changeType": "updated", "revision": str(i + 1),
             "file": {"id": str(i + 1), "type": "file", "version": 0,
                      "name": f"f{i}.txt", "path": "/", "hash": None, "size": 1}}
            for i in range(total)
        ]
        self.chunk = chunk

    def api(self, method, path, **kw):
        params = kw.get("params") or {}
        after = int(params.get("latestRevision") or 0)
        rest = [it for it in self.items if int(it["revision"]) > after]
        return {"header": {"isSuccessful": True, "resultCode": 0},
                "result": rest[: self.chunk]}   # 요청 size보다 적게 반환


def test_iter_changes_terminates_only_on_empty_page():
    """R11: len(items) < size로 끊으면 뒤의 변경을 영구 누락한다.

    이 함정이 PoC-05 1차 실행을 통째로 실패시켰다.
    """
    from dooray_sync.api.drive import DriveAPI

    drive = DriveAPI(_FakeChangesClient(total=250, chunk=40))   # size=200 요청, 40건씩 반환
    got = [ci for ci, _ in drive.iter_changes("d1", Cursor(), size=200)]
    assert len(got) == 250, f"부분 페이지에서 조기 종료됨: {len(got)}건만 수집"


# ------------------------------------------------- 이름 대소문자 (2026-08-02 실측 결함)
class _FakeDriveClient:
    """목록·폴더생성만 흉내 내는 최소 클라이언트.

    서버 실측 특성 재현: 이름 중복 검사는 **대소문자를 무시**한다 →
    'Writing'을 만들려 해도 'WRITING'이 있으면 409 Duplicate request.
    """

    def __init__(self, children):
        import logging
        self.children = list(children)
        self.logger = logging.getLogger("fake")
        self.created = []

    def api(self, method, path, **kw):
        if path.endswith("/create-folder"):
            name = (kw.get("json") or {}).get("name", "")
            from dooray_sync.api.client import DoorayApiError
            if any(c["name"].casefold() == name.casefold() for c in self.children):
                raise DoorayApiError("Duplicate request", status=409, path=path)
            self.created.append(name)
            item = {"id": f"new-{name}", "name": name, "type": "folder"}
            self.children.append(item)
            return {"header": {"isSuccessful": True, "resultCode": 0}, "result": item}
        page = int((kw.get("params") or {}).get("page") or 0)
        size = int((kw.get("params") or {}).get("size") or 100)
        chunk = self.children[page * size:(page + 1) * size]
        return {"header": {"isSuccessful": True, "resultCode": 0},
                "result": chunk, "totalCount": len(self.children)}


def test_find_child_by_name_falls_back_to_case_insensitive():
    """서버의 중복 검사는 대소문자를 무시한다 — 정확 일치만 보면 원격 폴더를 못 찾고
    새로 만들려다 409 Duplicate request로 영원히 막힌다(2026-08-02 실측)."""
    from dooray_sync.api.drive import DriveAPI

    drive = DriveAPI(_FakeDriveClient([
        {"id": "1", "name": "WRITING", "type": "folder"},
        {"id": "2", "name": "Report.txt", "type": "file"},
    ]))
    assert drive.find_child_by_name("d", "p", "Writing").id == "1"
    assert drive.find_child_by_name("d", "p", "report.TXT").id == "2"
    assert drive.find_child_by_name("d", "p", "없는이름") is None


def test_find_child_by_name_prefers_exact_match():
    """대소문자만 다른 항목이 공존해도 정확 일치가 우선이어야 판정이 흔들리지 않는다."""
    from dooray_sync.api.drive import DriveAPI

    drive = DriveAPI(_FakeDriveClient([
        {"id": "lower", "name": "data.txt", "type": "file"},
        {"id": "upper", "name": "DATA.TXT", "type": "file"},
    ]))
    assert drive.find_child_by_name("d", "p", "DATA.TXT").id == "upper"
    assert drive.find_child_by_name("d", "p", "data.txt").id == "lower"


def test_create_folder_absorbs_409_as_existing_folder():
    """폴더 생성은 멱등해야 한다. 409를 그대로 올리면 init --create-remote가
    원인 없는 오류로 계속 실패한다. 새로 만든 것인지 여부도 알려 줘야 한다 —
    기존 폴더를 '방금 만들어 비어 있다'고 오인하면 하위를 통째로 놓친다."""
    from dooray_sync.api.drive import DriveAPI

    client = _FakeDriveClient([{"id": "1", "name": "WRITING", "type": "folder"}])
    drive = DriveAPI(client)

    found, is_new = drive.create_folder_ex("d", "p", "Writing")
    assert found.id == "1" and is_new is False
    assert client.created == [], "이미 있는 폴더를 다시 만들면 안 된다"

    made, is_new = drive.create_folder_ex("d", "p", "새폴더")
    assert is_new is True and made.name == "새폴더"


# ---------------------------------------------------------------- 스캐너
def test_scanner_preserves_disk_path_for_nfd_names(tmp_path: Path):
    """NTFS는 이름을 정규화하지 않는다 — NFC로 바꾼 경로로는 NFD 파일을 못 연다."""
    nfd_name = unicodedata.normalize("NFD", "한글파일.txt")
    target = tmp_path / nfd_name
    with open(ext_path(target), "wb") as f:
        f.write(b"hello")

    scanner = LocalScanner(tmp_path, [])
    entries = scanner.scan()
    assert len(entries) == 1
    entry = next(iter(entries.values()))

    # rel_path는 비교용이라 NFC로 정규화돼 있다
    assert entry.rel_path == unicodedata.normalize("NFC", entry.rel_path)
    # 그런데 해시는 실제로 열려야 한다 — disk_path가 없으면 여기서 FileNotFoundError
    filled = scanner.fill_md5(entry)
    assert filled.md5 == "5d41402abc4b2a76b9719d911017c592"  # md5("hello")


def test_scanner_skips_excluded_and_tmp_dir(tmp_path: Path):
    (tmp_path / ".dooraysync_tmp").mkdir()
    with open(ext_path(tmp_path / ".dooraysync_tmp" / "x.part"), "wb") as f:
        f.write(b"1")
    with open(ext_path(tmp_path / "keep.txt"), "wb") as f:
        f.write(b"1")
    with open(ext_path(tmp_path / "skip.tmp"), "wb") as f:
        f.write(b"1")

    entries = LocalScanner(tmp_path, ["*.tmp"]).scan()
    names = {e.rel_path for e in entries.values()}
    assert "keep.txt" in names
    assert "skip.tmp" not in names
    assert not any(n.startswith(".dooraysync_tmp") for n in names)


def test_needs_hash_skips_unchanged(tmp_path: Path):
    """성능의 핵심 — (mtime,size)가 같으면 해시를 다시 계산하지 않는다."""
    with open(ext_path(tmp_path / "a.txt"), "wb") as f:
        f.write(b"x")
    scanner = LocalScanner(tmp_path, [])
    entry = next(iter(scanner.scan().values()))
    rec = FileRecord(drive_id="d", rel_path="a.txt", local_mtime_ns=entry.mtime_ns,
                     local_size=entry.size, local_md5="deadbeef")
    assert scanner.needs_hash(entry, rec) is False
    assert scanner.needs_hash(entry, None) is True


# ---------------------------------------------------------------- 상태 DB
def test_store_upsert_is_full_state_and_unique_by_key(tmp_path: Path):
    with Store(tmp_path / "s.db") as store:
        store.upsert_file(FileRecord(drive_id="d", rel_path="A/b.txt", file_id="1",
                                     local_md5="aa"))
        # 대소문자만 다른 경로는 같은 레코드여야 한다(Windows 의미론)
        store.upsert_file(FileRecord(drive_id="d", rel_path="a/B.TXT", file_id="1",
                                     local_md5="bb"))
        assert store.count_files("d") == 1
        rec = store.get_by_path("d", "A/b.txt")
        assert rec is not None and rec.local_md5 == "bb"


def test_store_cursor_roundtrip(tmp_path: Path):
    with Store(tmp_path / "s.db") as store:
        store.set_cursor(Cursor(revision=17514, file_id="abc"))
        cur = store.get_cursor()
        assert cur.revision == 17514 and cur.file_id == "abc"


def test_store_transaction_rolls_back(tmp_path: Path):
    with Store(tmp_path / "s.db") as store:
        store.upsert_file(FileRecord(drive_id="d", rel_path="a.txt", file_id="1"))
        try:
            with store.transaction():
                store.upsert_file(FileRecord(drive_id="d", rel_path="b.txt", file_id="2"))
                raise RuntimeError("의도적 실패")
        except RuntimeError:
            pass
        assert store.get_by_path("d", "b.txt") is None
        assert store.get_by_path("d", "a.txt") is not None


# ---------------------------------------------------------------- push 계획
def _entry(rel: str, *, md5: str, mtime: int = 111, size: int = 10):
    from dooray_sync.core.scanner import LocalEntry
    return LocalEntry(rel_path=rel, rel_path_key=path_key(rel), is_dir=False,
                      disk_path="", mtime_ns=mtime, size=size, md5=md5)


class _NoHashScanner:
    """fill_md5가 이미 채워진 값을 그대로 쓰도록 하는 최소 스캐너 대역."""

    def needs_hash(self, entry, rec):
        return False

    def fill_md5(self, entry):
        return entry


def test_push_holds_files_without_local_baseline():
    """init 직후처럼 로컬 기준선이 없으면 UPDATE를 내지 않는다.

    이걸 놓치면 첫 push가 오래된 로컬 사본으로 원격을 덮어쓴다.
    """
    from dooray_sync.cli.main import _plan_push
    from dooray_sync.config import Profile

    entries = {path_key("a.txt"): _entry("a.txt", md5="aaa")}
    base = {path_key("a.txt"): FileRecord(drive_id="d", rel_path="a.txt", file_id="F1",
                                          local_md5=None)}   # 원격은 아는데 로컬은 모름
    p = Profile(name="t", drive_id="d", local_root="C:\\tmp")

    plan = _plan_push(_NoHashScanner(), entries, base, p, None)
    assert plan.items == [], "기준선 없는 파일을 업로드 계획에 넣으면 원격이 덮인다"
    assert plan.ambiguous == ["a.txt"]

    # 명시적으로 허용하면 그때는 올린다
    forced = _plan_push(_NoHashScanner(), entries, base, p, None, assume_local_newer=True)
    assert [i.op for i in forced.items] == ["UPDATE"]


def test_push_skips_unchanged_and_touches_mtime_only_change():
    from dooray_sync.cli.main import _plan_push
    from dooray_sync.config import Profile

    p = Profile(name="t", drive_id="d", local_root="C:\\tmp")

    same = {path_key("a.txt"): _entry("a.txt", md5="aaa", mtime=111, size=10)}
    base = {path_key("a.txt"): FileRecord(drive_id="d", rel_path="a.txt", file_id="F1",
                                          local_md5="aaa", local_mtime_ns=111, local_size=10)}
    assert _plan_push(_NoHashScanner(), same, base, p, None).skipped_same == 1

    touched = {path_key("a.txt"): _entry("a.txt", md5="aaa", mtime=222, size=10)}
    plan = _plan_push(_NoHashScanner(), touched, base, p, None)
    assert [i.op for i in plan.items] == ["TOUCH"], "내용이 같으면 재전송하지 않는다"


# ---------------------------------------------------------------- 설정 파일 안전
def test_read_doc_distinguishes_missing_from_unreadable(tmp_path: Path, monkeypatch=None):
    """os.path.exists는 잠긴 파일에도 False를 준다 — '없음'과 '못 읽음'을 구분해야 한다.

    구분하지 못하면 init이 설정을 새로 만들면서 다른 프로파일을 전부 날린다.
    """
    import os as _os
    from dooray_sync import config as cfg

    cfgdir = tmp_path / "cfgdir"
    cfgdir.mkdir()
    _os.environ[cfg.ENV_CONFIG_DIR] = str(cfgdir)
    try:
        # 1) 진짜 없을 때는 빈 dict
        assert cfg._read_doc() == {}

        # 2) 정상 파일은 읽힌다
        cfg.save_config(cfg.Profile(name="a", drive_id="D1", local_root=str(tmp_path)))
        cfg.save_config(cfg.Profile(name="b", drive_id="D2", local_root=str(tmp_path)))
        assert set((cfg._read_doc().get("profile") or {})) == {"a", "b"}

        # 3) 읽기 실패를 '없음'으로 삼키지 않는다
        real_open = __builtins__["open"] if isinstance(__builtins__, dict) else __builtins__.open

        def locked(path, *a, **kw):
            if str(path).endswith("config.toml"):
                raise PermissionError(13, "다른 프로세스가 사용 중")
            return real_open(path, *a, **kw)

        import builtins
        builtins.open = locked
        try:
            raised = False
            try:
                cfg._read_doc()
            except RuntimeError:
                raised = True
            assert raised, "잠긴 설정 파일을 '없음'으로 처리하면 안 된다"
        finally:
            builtins.open = real_open

        # 4) 두 프로파일이 여전히 살아 있다
        assert set((cfg._read_doc().get("profile") or {})) == {"a", "b"}
    finally:
        _os.environ.pop(cfg.ENV_CONFIG_DIR, None)


def _run_all() -> int:
    """pytest 없이도 돌 수 있게 — tmp_path 인자는 임시 디렉터리로 채운다."""
    import inspect
    import tempfile
    import traceback

    fails = 0
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for name, fn in fns:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} 통과")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(_run_all())
