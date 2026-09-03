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
from dooray_sync.cli.main import _normalize_keep, _prompt_keep   # noqa: E402

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
