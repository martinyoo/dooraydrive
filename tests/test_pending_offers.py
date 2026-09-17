"""동기화 종료 후 보류 항목을 같은 창에서 해소하는 경로 (2026-09-04 사용자 요구).

지금까지 sync는 "다른 명령을 실행하세요"로 끝났다 — 대량 삭제는
`--allow-bulk-delete`로 재실행, 기준선 없음은 `dsync reconcile`. 사용자가 터미널을
새로 열어 명령을 치게 하지 말고, 모든 프로파일이 끝난 뒤 같은 창에서 묻는다.

여기서 지키는 불변식 둘:
  1. **묻는 것은 부수 기능이다.** 어떤 실패도 종료코드를 바꾸지 않고, 어떤 입력
     실패(EOF·None 스트림·Ctrl+C)도 '아니오'로 떨어진다. Windows에서 NUL은 문자
     장치라 stdin=DEVNULL에서도 isatty()가 True이므로(2026-08-21 실측) 판정을
     isatty에 걸 수 없다 — 기본값 '아니오'인 질문 하나가 유일한 방어선이다.
  2. **감지는 화면이 아니라 --report-json의 outcome으로 한다.** 문구가 바뀌어도
     안 깨지게.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dooray_sync import config as cfg   # noqa: E402
from dooray_sync.cli.main import (                              # noqa: E402
    _ask_apply_rest, _normalize_keep, _prompt_keep,
)

_SH = None


def _sync_here():
    global _SH
    if _SH is None:
        spec = importlib.util.spec_from_file_location(
            "sync_here_offers_under_test", REPO / "tools" / "sync_here.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SH = mod
    return _SH


# --------------------------------------------------------------------------
# 1) 충돌 선택지 숫자화 — 1/2/3과 이름을 둘 다 받는다
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("1", "both"), ("2", "local"), ("3", "remote"),
    ("both", "both"), ("local", "local"), ("remote", "remote"),
    (" 2 ", "local"), ("BOTH", "both"),          # 공백·대문자 허용
    ("", ""), ("4", ""), ("0", ""), ("l", ""), ("yes", ""),
])
def test_keep_accepts_numbers_and_names(raw, want):
    assert _normalize_keep(raw) == want


def test_keep_rejects_out_of_range_so_caller_can_reask():
    """오입력이 ''로 떨어져야 호출측이 '건너뛰기' 대신 재질문할 수 있다."""
    assert _normalize_keep("9") == ""
    assert _normalize_keep(None) == ""


def test_prompt_keep_empty_input_takes_default_both(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _p: "")
    assert _prompt_keep() == "both"


def test_prompt_keep_reasks_on_bad_input_then_accepts(monkeypatch):
    """예전에는 오타 한 번이면 그 충돌을 조용히 건너뛰었다 — 이제 되묻는다."""
    answers = iter(["9", "zzz", "3"])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert _prompt_keep() == "remote"


def test_prompt_keep_gives_up_after_tries_and_never_loops(monkeypatch):
    calls = []

    def _bad(_p):
        calls.append(1)
        return "nope"

    monkeypatch.setattr("builtins.input", _bad)
    assert _prompt_keep(tries=3) == ""
    assert len(calls) == 3          # 유한 — 무한 재질문 금지


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt, OSError, ValueError])
def test_prompt_keep_returns_empty_on_dead_stdin(monkeypatch, exc):
    """EOF·Ctrl+C·죽은 스트림 전부 즉시 포기 — 창이 영영 멈추면 안 된다."""
    def _boom(_p):
        raise exc

    monkeypatch.setattr("builtins.input", _boom)
    assert _prompt_keep() == ""


# --------------------------------------------------------------------------
# 1-b) 중도 이탈과 일괄 적용 (2026-09-18 사용자 요구)
#
# 실사용에서 3,869건을 한 건씩 누르다 중단했다. 화면이 "CLI 인자를 치세요"로
# 끝나면 그 기능은 없는 것이다(AGENTS.md) — 그래서 '0) 그만'과 '남은 전부 같게'를
# 넣었다. 둘 다 안전한 쪽이 기본값이어야 한다.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["0", "q", "Q", "quit", "그만", " 0 "])
def test_prompt_keep_quits_when_allowed(monkeypatch, raw):
    monkeypatch.setattr("builtins.input", lambda _p: raw)
    assert _prompt_keep(allow_quit=True) == "quit"


@pytest.mark.parametrize("raw", ["0", "q", "quit", "그만"])
def test_quit_words_are_not_accepted_without_allow_quit(monkeypatch, raw):
    """`--keep` 검증과 표를 공유하므로, 허락하지 않은 자리에서는 그냥 오입력이다."""
    answers = iter([raw, raw, raw])
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    assert _prompt_keep(tries=3) == ""


def test_keep_flag_never_accepts_quit():
    """`--keep 0` 이 통과하면 _resolve_one 에 알 수 없는 pick 이 들어간다."""
    for raw in ("0", "q", "quit", "그만"):
        assert _normalize_keep(raw) == ""


def test_quit_beats_default_so_enter_still_means_both(monkeypatch):
    """빈 입력은 여전히 기본값 both — '그만'이 기본이 되면 안 된다."""
    monkeypatch.setattr("builtins.input", lambda _p: "")
    assert _prompt_keep(allow_quit=True) == "both"


@pytest.mark.parametrize("ans,want", [
    ("y", True), ("yes", True), ("Y", True), ("예", True),
    ("", False), ("n", False), ("no", False), ("아니오", False), ("2", False),
])
def test_apply_rest_defaults_to_no(monkeypatch, ans, want):
    monkeypatch.setattr("builtins.input", lambda _p: ans)
    assert _ask_apply_rest("local", 3868) is want


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt, OSError, ValueError])
def test_apply_rest_is_no_on_dead_stdin(monkeypatch, exc):
    """타입어헤드·EOF·Ctrl+C 가 3,869건을 움직이면 안 된다."""
    def _boom(_p):
        raise exc

    monkeypatch.setattr("builtins.input", _boom)
    assert _ask_apply_rest("local", 3868) is False


def test_apply_rest_not_asked_when_nothing_remains(monkeypatch):
    def _never(_p):
        raise AssertionError("남은 건이 없는데 물었다")

    monkeypatch.setattr("builtins.input", _never)
    assert _ask_apply_rest("local", 0) is False


def test_apply_rest_shows_the_count_and_the_choice(monkeypatch, capsys):
    """무엇을 몇 건에 적용하는지 보이지 않으면 동의가 아니다."""
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    _ask_apply_rest("local", 3868)
    out = capsys.readouterr().out
    assert "3868" in out
    assert "local" in out


def test_apply_rest_survives_none_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)
    assert _ask_apply_rest("local", 10) is False


# --------------------------------------------------------------------------
# 1-c) 루프 배선 — 헬퍼가 맞아도 이어 붙이는 곳에서 틀릴 수 있다
# --------------------------------------------------------------------------
class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def iter_unresolved(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _drive_resolve(monkeypatch, answers, n_rows=5):
    """resolve 를 대화형으로 돌리고 (처리된 id 목록, 선택 목록, 화면)을 돌려준다."""
    import contextlib

    from dooray_sync.cli import main as m

    rows = [{"id": i, "rel_path": f"f{i}.txt", "kind": "both_modified",
             "local_copy_path": f"C:\\x\\f{i} (충돌).txt", "ts": "2026-09-18"}
            for i in range(1, n_rows + 1)]
    handled = []

    monkeypatch.setattr(m, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(m, "_error_boundary", lambda _log: contextlib.nullcontext())
    monkeypatch.setattr(m, "_instance_lock", lambda _n: contextlib.nullcontext())
    monkeypatch.setattr(m, "_load_profile",
                        lambda n: cfg.Profile(name=n, drive_id="", local_root="C:\\x"))
    monkeypatch.setattr(m, "db_path", lambda _n: ":memory:")
    monkeypatch.setattr(m, "Store", lambda _p: _FakeStore(rows))
    monkeypatch.setattr(m, "_table", lambda *a, **k: None)
    monkeypatch.setattr(
        m, "_resolve_one",
        lambda store, p, row, pick, dry, log, drive=None: (
            handled.append((int(row["id"]), pick)) or True))

    # resolve 는 대화형이 아니면 --keep 을 요구하며 죽는다. 여기서는 사람이 보고
    # 있다고 가정한다 — isatty 판정 자체의 회귀는 별도 테스트가 본다.
    class _TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(m.sys, "stdin", _TTY())
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _p: next(it))
    m.resolve(profile="t", list_only=False, conflict_id=0, keep="",
              dry_run=False, verbose=False)
    return handled


def test_zero_stops_the_loop_and_leaves_the_rest(monkeypatch, capsys):
    """'0'을 고르면 거기서 멈춘다 — 예전에는 빠져나갈 길이 없었다."""
    handled = _drive_resolve(monkeypatch, ["2", "n", "0"], n_rows=5)
    out = capsys.readouterr().out

    assert handled == [(1, "local")], handled
    assert "중단했습니다" in out
    assert "4건" in out, "남은 건수를 알려 주지 않으면 이어서 할지 판단할 수 없다"


def test_apply_rest_processes_everything_without_more_prompts(monkeypatch):
    """1건 처리 → '전부 같게' 동의 → 남은 4건은 묻지 않고 같은 선택으로."""
    # 답이 둘뿐인데 5건이다 — 세 번째 input 이 호출되면 StopIteration 으로 죽는다.
    handled = _drive_resolve(monkeypatch, ["2", "y"], n_rows=5)

    assert handled == [(i, "local") for i in range(1, 6)], handled


def test_apply_rest_declined_keeps_asking_each_time(monkeypatch):
    """거절하면 예전대로 한 건씩 묻는다 — 한 번 거절이 '전부 건너뛰기'가 아니다."""
    handled = _drive_resolve(monkeypatch, ["2", "n", "1", "3"], n_rows=3)

    assert handled == [(1, "local"), (2, "both"), (3, "remote")], handled


def test_apply_rest_is_asked_once_only(monkeypatch, capsys):
    """매 건마다 되물으면 그것도 3,869번이다."""
    _drive_resolve(monkeypatch, ["2", "n", "2", "2"], n_rows=3)
    out = capsys.readouterr().out

    # '남은'은 메뉴의 '0) 그만' 줄에도 있다 — _ask_apply_rest 에만 있는 문장을 센다.
    assert out.count("되돌릴 수 있습니다") == 1, out


def test_progress_is_shown_so_the_end_is_visible(monkeypatch, capsys):
    """끝이 보이지 않는 프롬프트가 사용자를 중단시켰다."""
    _drive_resolve(monkeypatch, ["2", "y"], n_rows=5)
    out = capsys.readouterr().out

    assert "(1/5)" in out, out


def test_keep_flag_skips_every_prompt(monkeypatch):
    """--keep 이 있으면 묻지 않는다 — 있던 계약이 그대로여야 한다."""
    import contextlib

    from dooray_sync.cli import main as m

    rows = [{"id": i, "rel_path": f"f{i}.txt", "kind": "both_modified",
             "local_copy_path": f"C:\\x\\f{i} (충돌).txt", "ts": "2026-09-18"}
            for i in range(1, 4)]
    handled = []

    def _never(_p):
        raise AssertionError("--keep 을 줬는데 물었다")

    monkeypatch.setattr(m, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(m, "_error_boundary", lambda _log: contextlib.nullcontext())
    monkeypatch.setattr(m, "_instance_lock", lambda _n: contextlib.nullcontext())
    monkeypatch.setattr(m, "_load_profile",
                        lambda n: cfg.Profile(name=n, drive_id="", local_root="C:\\x"))
    monkeypatch.setattr(m, "db_path", lambda _n: ":memory:")
    monkeypatch.setattr(m, "Store", lambda _p: _FakeStore(rows))
    monkeypatch.setattr(m, "_table", lambda *a, **k: None)
    monkeypatch.setattr(
        m, "_resolve_one",
        lambda store, p, row, pick, dry, log, drive=None: (
            handled.append((int(row["id"]), pick)) or True))
    monkeypatch.setattr("builtins.input", _never)

    m.resolve(profile="t", list_only=False, conflict_id=0, keep="local",
              dry_run=False, verbose=False)
    assert handled == [(1, "local"), (2, "local"), (3, "local")]


# --------------------------------------------------------------------------
# 2) 기준선 없음 집계 — differ._has_baseline과 같은 술어
# --------------------------------------------------------------------------
def _make_db(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE files (id INTEGER PRIMARY KEY, drive_id TEXT, file_id TEXT,"
        " rel_path TEXT, is_dir INTEGER, local_md5 TEXT)")
    conn.executemany(
        "INSERT INTO files (drive_id, file_id, rel_path, is_dir, local_md5)"
        " VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture()
def dbdir(tmp_path, monkeypatch):
    """상태 DB 루트를 tmp로 옮긴다 — 실제 %LOCALAPPDATA%를 건드리지 않는다."""
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(tmp_path / "state"))
    return tmp_path


def test_baseline_missing_counts_only_files_without_local_md5(dbdir):
    sh = _sync_here()
    _make_db(cfg.db_path("P"), [
        ("d", "f1", "a.md", 0, None),      # 기준선 없음 → 셈
        ("d", "f2", "b.md", 0, ""),        # 빈 문자열도 없음 → 셈
        ("d", "f3", "c.md", 0, "abc"),     # 기준선 있음 → 제외
        ("d", "f4", "dir", 1, None),       # 폴더 → 제외
        ("d", "",   "e.md", 0, None),      # 원격 상대 없음 → 제외
    ])
    assert sh._baseline_missing_count("P") == 2


def test_baseline_missing_is_zero_when_db_absent(dbdir):
    """DB가 없어도 0을 낸다 — Store를 쓰면 빈 DB를 만들어 무인 게이트를 무력화한다."""
    sh = _sync_here()
    assert sh._baseline_missing_count("absent-profile") == 0
    assert not cfg.db_path("absent-profile").exists()


def test_baseline_missing_swallows_broken_db(dbdir):
    sh = _sync_here()
    p = cfg.db_path("P")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not a database")
    assert sh._baseline_missing_count("P") == 0


# --------------------------------------------------------------------------
# 3) 대량 삭제 감지 — 화면이 아니라 report-json의 outcome
# --------------------------------------------------------------------------
def test_bulk_delete_detected_from_report_outcome(tmp_path):
    sh = _sync_here()
    sh._PENDING_DELETES.clear()
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps({
        "outcome": "aborted_bulk_delete",
        "error": "대량 삭제로 판단해 중단했습니다: 삭제 대상 80건 >= 임계 50건\n둘째 줄",
    }), encoding="utf-8")
    sh._note_bulk_delete("P", r"D:\x", str(rep))
    assert len(sh._PENDING_DELETES) == 1
    name, root, why = sh._PENDING_DELETES[0]
    assert (name, root) == ("P", r"D:\x")
    assert "80건" in why and "\n" not in why      # 첫 줄만 요약으로 쓴다
    sh._PENDING_DELETES.clear()


@pytest.mark.parametrize("payload", [
    {"outcome": "partial"}, {"outcome": "ok"}, {}, {"outcome": ""},
])
def test_other_outcomes_are_not_bulk_delete(tmp_path, payload):
    sh = _sync_here()
    sh._PENDING_DELETES.clear()
    rep = tmp_path / "r.json"
    rep.write_text(json.dumps(payload), encoding="utf-8")
    sh._note_bulk_delete("P", "root", str(rep))
    assert sh._PENDING_DELETES == []


def test_unreadable_report_is_ignored(tmp_path):
    """보고서가 깨졌거나 없어도 조용히 넘어간다 — 안내가 동기화를 죽이면 안 된다."""
    sh = _sync_here()
    sh._PENDING_DELETES.clear()
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    sh._note_bulk_delete("P", "root", str(bad))
    sh._note_bulk_delete("P", "root", str(tmp_path / "missing.json"))
    assert sh._PENDING_DELETES == []


# --------------------------------------------------------------------------
# 4) 무인·비대화 경로 보호 — 가장 중요한 회귀
# --------------------------------------------------------------------------
@pytest.mark.parametrize("flag", ["--dry-run", "--unattended"])
def test_offers_never_run_children_in_unattended_paths(monkeypatch, capsys, flag):
    """dry-run·무인에서는 알리기만 하고 자식을 절대 띄우지 않는다."""
    sh = _sync_here()
    called = []
    monkeypatch.setattr(sh, "_run_child", lambda *a, **k: called.append(a) or 0)
    monkeypatch.setattr(sh, "_ask_yes_no",
                        lambda q: pytest.fail("무인 경로에서 질문이 떴다"))

    sh._PENDING_DELETES[:] = [("P", "root", "임계 초과")]
    sh._offer_bulk_delete([flag])
    sh._PENDING_BASELINE[:] = [("P", "root", 3)]
    sh._offer_reconcile([flag])

    assert called == []
    out = capsys.readouterr().out
    assert "P" in out                      # 알림 자체는 남는다


def test_answering_no_runs_nothing(monkeypatch, capsys):
    sh = _sync_here()
    called = []
    monkeypatch.setattr(sh, "_run_child", lambda *a, **k: called.append(a) or 0)
    monkeypatch.setattr(sh, "_ask_yes_no", lambda q: False)

    sh._PENDING_BASELINE[:] = [("P", "root", 3)]
    sh._offer_reconcile([])
    assert called == []
    assert "나중에 하려면" in capsys.readouterr().out


def test_eof_stdin_answers_no_and_does_not_hang(monkeypatch):
    """EOF는 '아니오'다. isatty가 True인 DEVNULL 환경이 실재한다."""
    sh = _sync_here()

    def _eof(_q):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert sh._ask_yes_no("q: ") is False


def test_offer_failure_never_raises(monkeypatch, capsys):
    """안내 중 예외가 나도 삼킨다 — 동기화 결과(종료코드)를 뒤집으면 안 된다."""
    sh = _sync_here()

    def _boom(_q):
        raise RuntimeError("boom")

    monkeypatch.setattr(sh, "_ask_yes_no", _boom)
    sh._PENDING_BASELINE[:] = [("P", "root", 1)]
    sh._offer_reconcile([])          # 예외가 밖으로 나오면 테스트 실패
    assert "영향 없음" in capsys.readouterr().out


def test_pending_lists_are_drained_so_second_call_is_silent(monkeypatch, capsys):
    """한 번 물은 것은 비운다 — 같은 실행에서 두 번 묻지 않는다."""
    sh = _sync_here()
    monkeypatch.setattr(sh, "_ask_yes_no", lambda q: False)
    sh._PENDING_BASELINE[:] = [("P", "root", 1)]
    sh._offer_reconcile([])
    capsys.readouterr()
    sh._offer_reconcile([])
    assert capsys.readouterr().out == ""
