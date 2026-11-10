"""대량 삭제 보류를 자동 동기화 창에서 y/N 으로 처리하는 경로 (2026-11-10 사용자 요구).

자동 동기화 창은 대량 삭제 게이트로 멈춘 폴더에 대해 "--allow-bulk-delete 를 붙여 다시
실행하세요"만 찍고 같은 보류를 240틱 넘게 반복했다(WRITING 실측). 사람이 다른 창을 열어
명령을 쳐야 했다 — AGENTS.md "화면이 명령을 시키면 그 기능은 없는 것이다".

여기서 지키는 불변식 (AGENTS.md 「자동 동기화 창의 대량 삭제 — y 로 여는 질문」과 짝):
  1. 루프를 붙잡지 않는다 — 키는 대기 중에만, 보류가 있을 때만 본다. 보류가 없으면
     키를 읽지도 않는다.
  2. 먼저 보여 주고, 몇 건인지 적고, 기본값 '아니오'로 묻는다. 시간 초과·Ctrl+C·Esc 도
     '아니오'.
  3. 본 계획만 실행한다 — 미리보기의 삭제 지문이 실행 때와 다르면 CLI 가 아무것도 하지
     않는다(--expect-delete-fp).
  4. 무인 강등(--unattended)은 사람이 동의한 뒤에도 그대로다.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import typer

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dooray_sync import config as cfg                            # noqa: E402
from dooray_sync.auto import keys, notify, runner                # noqa: E402
from dooray_sync.auto.state import AutoState, auto_dir           # noqa: E402
from dooray_sync.core.differ import Decision                     # noqa: E402
from dooray_sync.core.planner import (                           # noqa: E402
    BULK_DELETE_FLAG_HINT, KIND_LOCAL_TRASH, BulkDeleteAbort, plan,
)
from dooray_sync.util.paths import ext_path, path_key            # noqa: E402

_SH = None


def _sync_here():
    global _SH
    if _SH is None:
        spec = importlib.util.spec_from_file_location(
            "sync_here_bulk_under_test", REPO / "tools" / "sync_here.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SH = mod
    return _SH


# ==========================================================================
# 1) planner — 판정 근거를 속성으로, 안내 줄만 갈아 끼우기
# ==========================================================================
def _abort(rel: str = "Work", n: int = 60) -> BulkDeleteAbort:
    d = Decision(case=8, kind=KIND_LOCAL_TRASH, rel_path=rel, key=path_key(rel), is_dir=True)
    base_keys = [path_key(rel)] + [path_key(f"{rel}/f{i}") for i in range(n)]
    with pytest.raises(BulkDeleteAbort) as ei:
        plan([d], base_count=len(base_keys), p=cfg.Profile(), base_keys=base_keys)
    return ei.value


def test_abort_carries_counts_and_why():
    exc = _abort(n=60)
    assert exc.count == 61 and exc.actions == 1
    assert "61건" in exc.why and "\n" not in exc.why


def test_abort_text_swaps_only_the_hint_line():
    exc = _abort()
    assert BULK_DELETE_FLAG_HINT in str(exc)                # 사람이 친 dsync 는 예전 그대로
    swapped = exc.text("  [창에서 y]")
    assert "[창에서 y]" in swapped and "--allow-bulk-delete" not in swapped
    assert swapped.splitlines()[0] == str(exc).splitlines()[0]


def test_hint_slot_cannot_collide_with_a_file_name():
    """파일 이름이 우연히 '{HINT}' 여도 그 자리가 바뀌면 안 된다."""
    exc = _abort(rel="{HINT}")
    assert "{HINT}" in exc.text("  X")


# ==========================================================================
# 2) CLI — 보고서에 보류 사유·지문, 무인 안내, 지문이 다르면 아무것도 안 함
# ==========================================================================
@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    from tests.test_m2 import _FakeDrive, _write
    from dooray_sync.cli import main as cli

    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(tmp_path / "cfg"))
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(tmp_path / "state"))
    monkeypatch.setenv("DOORAY_API_TOKEN", "테스트토큰" + "x" * 30)
    root = tmp_path / "local"
    os.makedirs(ext_path(root), exist_ok=True)

    drive = _FakeDrive()
    folder = drive.put("D", "root", type_="folder")
    for i in range(8):
        drive.put(f"d{i}.txt", folder, f"D{i}".encode())
    for i in range(30):                      # 로컬 붕괴 보류(50%)에 걸리지 않을 만큼
        drive.put(f"keep{i}.txt", "root", f"K{i}".encode())
    xdir = drive.put("X", "root", type_="folder")   # 이미 동기화된 다른 폴더(옮겨 넣기용)
    for i in range(3):
        drive.put(f"x{i}.txt", xdir, b"X")
    drive.x_folder = xdir

    @contextlib.contextmanager
    def fake_api(p, log):
        yield drive

    monkeypatch.setattr(cli, "_drive_api", fake_api)
    cfg.save_config(cfg.Profile(name="bd", drive_id="d", local_root=str(root),
                                bulk_delete_abort_count=5, bulk_delete_abort_ratio=0))

    def run(*, report: Path, **kw) -> int:
        args = dict(profile="bd", dry_run=False, full=True, propagate_deletes=False,
                    allow_bulk_delete=False, md5_probes=200, verbose=False,
                    report_json=str(report))
        args.update(kw)
        try:
            cli.sync(**args)
        except (SystemExit, typer.Exit) as exc:
            return int(getattr(exc, "exit_code", None) or getattr(exc, "code", 0) or 0)
        return 0

    assert run(report=tmp_path / "first.json") == 0
    shutil.rmtree(ext_path(root / "D"))          # 로컬에서 폴더 통째 삭제 → 원격 9건 삭제 계획
    return run, drive, folder, tmp_path, _write


def _rep(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_bulk_abort_report_has_reason_and_counts(cli_env):
    run, drive, folder, tmp, _ = cli_env
    rc = run(report=tmp / "a.json")
    rep = _rep(tmp / "a.json")
    assert rc == 1
    assert rep["outcome"] == "aborted_bulk_delete" and rep["held_reason"] == "bulk_delete"
    assert rep["bulk_delete"]["count"] == 9 and rep["bulk_delete"]["actions"] == 1
    assert "--allow-bulk-delete" in rep["error"]           # 사람이 친 dsync 는 플래그 안내
    assert folder not in drive.trashed


def test_unattended_abort_points_to_the_window_not_a_flag(cli_env):
    run, _drive, _folder, tmp, _ = cli_env
    run(report=tmp / "u.json", unattended=True)
    err = _rep(tmp / "u.json")["error"]
    assert "자동 동기화 창에서 y" in err
    assert "--allow-bulk-delete" not in err and "dsync " not in err     # 명령이 한 줄도 없다


def test_unattended_dry_run_does_not_promise_a_y(cli_env):
    run, _drive, _folder, tmp, _ = cli_env
    run(report=tmp / "d.json", unattended=True, dry_run=True)
    err = _rep(tmp / "d.json")["error"]
    assert "dry-run" in err and "y 를 누르" not in err and "dsync " not in err


def test_preview_reports_the_delete_fingerprint(cli_env):
    run, drive, folder, tmp, _ = cli_env
    assert run(report=tmp / "p.json", dry_run=True, allow_bulk_delete=True,
               unattended=True) == 0
    dels = _rep(tmp / "p.json")["deletes"]
    assert dels["count"] == 9 and len(dels["fp"]) == 16
    assert folder not in drive.trashed                     # 미리보기는 아무것도 안 바꾼다


def test_wrong_fingerprint_runs_nothing(cli_env):
    run, drive, folder, tmp, _ = cli_env
    rc = run(report=tmp / "x.json", allow_bulk_delete=True, unattended=True,
             expect_delete_fp="0000000000000000")
    rep = _rep(tmp / "x.json")
    assert rc == 1
    assert rep["outcome"] == "aborted_plan_changed" and rep["held_reason"] == "plan_changed"
    assert rep["fault"] == "held"
    assert folder not in drive.trashed, "동의하지 않은 계획을 실행했다"


def test_plan_change_between_preview_and_run_is_refused(cli_env):
    """미리보기 뒤에 삭제가 늘면(사람이 읽는 동안) 그 동의로는 실행하지 않는다."""
    run, drive, folder, tmp, _ = cli_env
    run(report=tmp / "p.json", dry_run=True, allow_bulk_delete=True, unattended=True)
    fp = _rep(tmp / "p.json")["deletes"]["fp"]
    root = Path(tmp / "local")
    os.remove(ext_path(root / "keep0.txt"))                # 그 사이 한 건 더 사라짐
    rc = run(report=tmp / "r.json", allow_bulk_delete=True, unattended=True,
             expect_delete_fp=fp)
    assert rc == 1 and _rep(tmp / "r.json")["outcome"] == "aborted_plan_changed"
    assert folder not in drive.trashed


@pytest.mark.parametrize("change", ["unsyncable_file", "unsyncable_folder", "moved_in"])
def test_remote_arrivals_inside_the_trashed_folder_are_refused(cli_env, change):
    """미리보기 뒤 원격 폴더 안으로 들어온 것 — 이 PC 에 담을 수 없는 이름, 옮겨 들어온
    폴더 — 은 폴더 삭제를 막지 않아 옛 동의로 함께 휴지통에 갔다(검토 재현). 이제 거부."""
    run, drive, folder, tmp, _ = cli_env
    run(report=tmp / "p.json", dry_run=True, allow_bulk_delete=True, unattended=True)
    fp = _rep(tmp / "p.json")["deletes"]["fp"]
    if change == "unsyncable_file":
        drive.put("Report: final.docx", folder, b"MAC")
    elif change == "unsyncable_folder":
        q = drive.put("Q3: plans", folder, type_="folder")
        drive.put("plan0.docx", q, b"P")
    else:
        drive.move("d", drive.x_folder, folder)     # 기준선에 있는 폴더 X 를 D 안으로
    rc = run(report=tmp / "r.json", allow_bulk_delete=True, unattended=True,
             expect_delete_fp=fp)
    assert rc == 1 and _rep(tmp / "r.json")["outcome"] == "aborted_plan_changed"
    assert folder not in drive.trashed


@pytest.mark.parametrize("name", ["D0.TXT", "~$minutes.docx"])
def test_hidden_remote_arrivals_inside_the_trashed_folder_are_refused(cli_env, name):
    """이름이 겹쳐 가려지는 파일(대소문자만 다름)·제외 규칙에 걸린 파일도 폴더와 함께
    휴지통에 간다 — 뷰에 넣지 않을 뿐 관측은 남겨 지문을 바꾼다(재검토 재현)."""
    run, drive, folder, tmp, _ = cli_env
    run(report=tmp / "p.json", dry_run=True, allow_bulk_delete=True, unattended=True)
    fp = _rep(tmp / "p.json")["deletes"]["fp"]
    drive.put(name, folder, b"HIDDEN")
    rc = run(report=tmp / "r.json", allow_bulk_delete=True, unattended=True,
             expect_delete_fp=fp)
    assert rc == 1 and _rep(tmp / "r.json")["outcome"] == "aborted_plan_changed"
    assert folder not in drive.trashed


@pytest.mark.parametrize("name", ["D0.TXT", "~$minutes.docx", "Report: final.docx"])
def test_full_preview_counts_hidden_items_in_the_folder(cli_env, name):
    run, drive, folder, tmp, _ = cli_env
    drive.put(name, folder, b"HIDDEN")
    run(report=tmp / "p.json", dry_run=True, allow_bulk_delete=True, unattended=True)
    assert _rep(tmp / "p.json")["deletes"]["extra_remote"] == 1


def test_preview_discloses_remote_items_the_pc_does_not_have(cli_env):
    """전체 순회 미리보기는 지울 원격 폴더 안의 '이 PC 에 없는 것'을 세어 보고한다."""
    run, drive, folder, tmp, _ = cli_env
    drive.put("Report: final.docx", folder, b"MAC")
    run(report=tmp / "p.json", dry_run=True, allow_bulk_delete=True, unattended=True)
    assert _rep(tmp / "p.json")["deletes"]["extra_remote"] >= 1


def test_matching_fingerprint_executes_the_seen_plan(cli_env):
    run, drive, folder, tmp, _ = cli_env
    run(report=tmp / "p.json", dry_run=True, allow_bulk_delete=True, unattended=True)
    fp = _rep(tmp / "p.json")["deletes"]["fp"]
    rc = run(report=tmp / "r.json", allow_bulk_delete=True, unattended=True,
             expect_delete_fp=fp)
    assert rc == 0 and _rep(tmp / "r.json")["outcome"] in ("ok", "partial")
    assert folder in drive.trashed                         # 원격은 휴지통으로


# ==========================================================================
# 3) bulk_delete_flow — 미리보기 → 묻기 → 본 계획만 실행 (사람 창·자동 창 공용)
# ==========================================================================
class _FakeRun:
    """run(args) 대역. --report-json 자리에 보고서를 쓴다."""

    def __init__(self, preview: dict | None = None, real: dict | None = None,
                 preview_rc: int = 0, real_rc: int = 0) -> None:
        self.preview = {"outcome": "dry_run", "deletes": {"fp": "abc", "count": 56}} \
            if preview is None else preview
        self.real = {"outcome": "ok"} if real is None else real
        self.preview_rc, self.real_rc = preview_rc, real_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> int:
        self.calls.append(list(args))
        dry = "--dry-run" in args
        out = args[args.index("--report-json") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(self.preview if dry else self.real, f, ensure_ascii=False)
        return self.preview_rc if dry else self.real_rc


def test_flow_declined_runs_only_the_preview(capsys):
    sh = _sync_here()
    run, asked = _FakeRun(), []
    result, _ = sh.bulk_delete_flow("W", run=run, ask=lambda q: asked.append(q) or False)
    assert result == sh.BULK_DECLINED
    assert len(run.calls) == 1 and "--dry-run" in run.calls[0]
    assert "56건" in asked[0]                               # 몇 건인지 적고 묻는다
    assert "아무것도 지우지 않았습니다" in capsys.readouterr().out


def test_flow_question_discloses_extra_remote_items(capsys):
    sh = _sync_here()
    run = _FakeRun(preview={"outcome": "dry_run",
                            "deletes": {"fp": "abc", "count": 9, "extra_remote": 7}})
    sh.bulk_delete_flow("W", run=run, ask=lambda q: False)
    assert "7건" in capsys.readouterr().out


def test_flow_yes_runs_the_seen_plan_with_same_base_args():
    sh = _sync_here()
    run = _FakeRun()
    result, rc = sh.bulk_delete_flow("W", run=run, ask=lambda q: True, base=["--unattended"])
    assert (result, rc) == (sh.BULK_DONE, 0)
    prev, real = run.calls
    assert prev[:2] == ["sync", "--unattended"] and real[:2] == ["sync", "--unattended"]
    assert "--allow-bulk-delete" in prev and "--allow-bulk-delete" in real
    assert "--dry-run" not in real
    assert real[real.index("--expect-delete-fp") + 1] == "abc"


@pytest.mark.parametrize("kw,want", [
    (dict(preview_rc=1), "preview_failed"),
    (dict(preview={"outcome": "dry_run", "deletes": {"count": 9}}), "preview_failed"),  # 지문 없음
    (dict(preview={"outcome": "aborted_bulk_delete"}), "preview_failed"),
    (dict(preview={"outcome": "dry_run", "deletes": {"fp": "abc", "count": 0}}), "nothing"),
])
def test_flow_never_asks_without_a_usable_preview(kw, want):
    sh = _sync_here()
    run = _FakeRun(**kw)
    result, _ = sh.bulk_delete_flow("W", run=run, ask=lambda q: pytest.fail("물으면 안 된다"))
    assert result == want and len(run.calls) == 1


@pytest.mark.parametrize("real,rc,want", [
    ({"outcome": "aborted_plan_changed"}, 1, "changed"),
    ({"outcome": "partial"}, 1, "done"),
    ({"outcome": "no_changes"}, 0, "done"),
    ({}, 1, "failed"),
])
def test_flow_reports_what_the_real_run_did(real, rc, want):
    sh = _sync_here()
    result, _ = sh.bulk_delete_flow("W", run=_FakeRun(real=real, real_rc=rc), ask=lambda q: True)
    assert result == want


def test_flow_keeps_callers_report_and_removes_its_temp(tmp_path, monkeypatch):
    sh = _sync_here()
    made = []
    real_new = sh._new_report_path
    monkeypatch.setattr(sh, "_new_report_path", lambda: made.append(real_new()) or made[-1])
    keep = tmp_path / "last.json"
    sh.bulk_delete_flow("W", run=_FakeRun(), ask=lambda q: True, report_path=str(keep))
    assert keep.exists()
    assert made and not any(os.path.exists(p) for p in made)


def test_manual_window_discards_typeahead_before_asking(monkeypatch):
    """미리보기 중에 눌린 y·Enter 가 input() 의 답이 되면 안 된다(검토에서 재현)."""
    sh = _sync_here()
    order = []
    monkeypatch.setattr(keys, "discard", lambda backend=None: order.append("discard"))
    monkeypatch.setattr(sh, "_ask_yes_no", lambda q: order.append("ask") or False)
    assert sh._ask_fresh("q? ") is False
    assert order == ["discard", "ask"]


def test_manual_offer_reports_resolved_profiles(monkeypatch):
    """풀린 프로파일은 실패가 아니다 — 배치가 [FAILED] 와 재시도를 띄우지 않게."""
    sh = _sync_here()
    fake = _FakeRun()
    monkeypatch.setattr(sh, "_run_child", lambda name, verb, *a: fake([verb, *a]))
    monkeypatch.setattr(sh, "_ask_fresh", lambda q: True)
    sh._PENDING_DELETES[:] = [("W", r"D:\w", "x"), ("V", r"D:\v", "x")]
    assert sh._offer_bulk_delete([]) == {"W", "V"}
    monkeypatch.setattr(sh, "_ask_fresh", lambda q: False)
    sh._PENDING_DELETES[:] = [("W", r"D:\w", "x")]
    assert sh._offer_bulk_delete([]) == set()
    assert sh._offer_bulk_delete([]) == set()                 # 비어 있어도 집합


@pytest.mark.parametrize("fake", [
    _FakeRun(real={"outcome": "partial"}, real_rc=1),        # 일부 실패 — 배치 재시도가 돌아야
    _FakeRun(preview={"outcome": "dry_run", "deletes": {"fp": "abc", "count": 0}}),  # 동기화 0번
])
def test_manual_offer_keeps_unfinished_profiles_failed(monkeypatch, fake):
    sh = _sync_here()
    fake.calls = []
    monkeypatch.setattr(sh, "_run_child", lambda name, verb, *a: fake([verb, *a]))
    monkeypatch.setattr(sh, "_ask_fresh", lambda q: True)
    sh._PENDING_DELETES[:] = [("W", r"D:\w", "x")]
    assert sh._offer_bulk_delete([]) == set()


def test_synchere_offer_uses_the_flow_and_no_longer_prints_commands(monkeypatch, capsys):
    """사람 창: 동의하면 지문을 들고 실행하고, 거절해도 명령을 치라고 하지 않는다."""
    sh = _sync_here()
    fake = _FakeRun()
    monkeypatch.setattr(sh, "_run_child", lambda name, verb, *a: fake([verb, *a]))
    monkeypatch.setattr(sh, "_ask_fresh", lambda q: False)
    sh._PENDING_DELETES[:] = [("W", r"D:\w", "삭제 대상 56건 ≥ 임계 50건")]
    sh._offer_bulk_delete([])
    out = capsys.readouterr().out
    assert len(fake.calls) == 1
    assert "dsync sync -p" not in out and "--allow-bulk-delete" not in out
    assert "synchere.bat" in out

    fake.calls.clear()
    monkeypatch.setattr(sh, "_ask_fresh", lambda q: True)
    sh._PENDING_DELETES[:] = [("W", r"D:\w", "삭제 대상 56건 ≥ 임계 50건")]
    sh._offer_bulk_delete([])
    assert "--expect-delete-fp" in fake.calls[-1]


# ==========================================================================
# 4) keys — 콘솔 입력 기록 단위 읽기, 시간 제한 있는 y/N
# ==========================================================================
KEY = keys.KEY_EVENT


def _rec(ch: str, down: bool = True, etype: int = KEY, rep: int = 1):
    return (etype, down, ch, rep)


def test_records_keep_only_key_down_characters():
    recs = [_rec("a"), _rec("a", down=False),            # 누름만
            _rec("\x00"), _rec("\x00", down=False),       # Shift+F6·화살표 — 글자 없음
            _rec("b", etype=2),                           # 마우스 사건
            _rec("à"),                                     # U+00E0 은 진짜 글자다
            _rec("z", rep=3)]
    assert keys.chars_from_records(recs) == "aàzzz"


class _Console:
    """콘솔 입력 대역 — 실제 백엔드처럼 **기록**을 쌓고, read 는 기록을 글자로 바꾼다.
    now 는 지금 쌓인 기록, later 는 첫 sleep 뒤에 도착하는 기록. flush 는 전부 버린다
    (특수키의 반쪽이 남는 msvcrt 와 달리 기록은 통째로 버려진다)."""

    def __init__(self, now=(), later=()) -> None:
        self.recs = [r if isinstance(r, tuple) else _rec(r) for r in now]
        self.later = [r if isinstance(r, tuple) else _rec(r) for r in later]

    def pending(self) -> int:
        return len(self.recs)

    def read(self) -> str:
        recs, self.recs = self.recs, []
        return keys.chars_from_records(recs)

    def flush(self) -> None:
        self.recs = []

    def arrive(self, _sec: float = 0) -> None:
        if self.later:
            self.recs.extend(self.later)
            self.later = []


SHIFT_F6 = [_rec("\x00"), _rec("\x00", down=False)]     # msvcrt 는 여기서 'Y' 를 남겼다


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _ask(con: _Console, *, timeout: float = 5, cancelled=lambda: False) -> str:
    clock = _Clock()

    def _sleep(sec: float) -> None:
        clock.t += sec
        con.arrive()

    return keys.ask("q? ", timeout=timeout, cancelled=cancelled, backend=con,
                    clock=clock, sleep=_sleep)


@pytest.mark.parametrize("typed,want", [
    ("y\r", "yes"), ("YES\r", "yes"), ("네\r", "yes"), ("예\r", "yes"),
    ("n\r", "no"), ("\r", "no"), ("yy\r", "no"),
    ("\x1b", "cancel"), ("\x03", "cancel"),
    ("n\x08y\r", "yes"),            # 백스페이스
])
def test_ask_answers(typed, want, capsys):
    assert _ask(_Console(later=list(typed))) == want


def test_special_keys_never_answer_yes():
    """Shift+F6 을 누른 뒤 Enter 만 치면 — msvcrt 시절에는 숨은 'Y' 로 '예'였다."""
    assert _ask(_Console(later=[*SHIFT_F6, _rec("\r")])) == "no"
    assert _ask(_Console(now=SHIFT_F6, later=[_rec("\r")])) == "no"
    assert _ask(_Console(later=[_rec("\x00"), _rec("y"), _rec("\r")])) == "yes"


def test_hangul_y_is_asked_again_not_taken_as_yes(capsys):
    """한글 입력기의 조합 중 'ㅛ' 는 다음 Enter 에 확정된다 — '예'로 받지 않고 되묻는다."""
    assert _ask(_Console(later=list("ㅛ\ry\r"))) == "yes"
    assert "영문 y" in capsys.readouterr().out
    assert _ask(_Console(later=list("ㅛ\rㅛ\rㅛ\r"))) == "no"      # 유한


def test_ask_discards_typeahead():
    """묻기 전에 쌓인 y·Enter 는 답이 아니다 — 답이 없으면 시간 초과(= 아니오)."""
    assert _ask(_Console(now=list("y\r"))) == "timeout"


def test_ask_times_out_to_no(capsys):
    assert _ask(_Console(), timeout=3) == "timeout"
    assert "'아니오'" in capsys.readouterr().out


def test_ask_cancelled_by_ctrl_c_flag():
    assert _ask(_Console(later=list("y\r")), cancelled=lambda: True) == "cancel"


def test_ask_without_console_is_no(monkeypatch, capsys):
    monkeypatch.setattr(keys, "_console", lambda: None)
    assert keys.ask("q? ", timeout=1) == "unavailable"
    assert keys.drain() == "" and keys.discard() is None


def test_ask_survives_a_bad_timeout_value():
    assert _ask(_Console(), timeout=None) == "timeout"


def test_drain_returns_typed_characters_only():
    assert keys.drain(_Console(now=[_rec("a"), *SHIFT_F6, _rec("b")])) == "ab"

    class _Broken:
        def pending(self):
            raise OSError("boom")

    assert keys.drain(_Broken()) == ""


# ==========================================================================
# 5) runner — 보류 세우기/지우기, 알림 문구, 대기 중 키, 처리
# ==========================================================================
NOW = runner._dt.datetime(2026, 11, 10, 10, 56)
BULK = {"outcome": "aborted_bulk_delete", "fault": "held", "held_reason": "bulk_delete",
        "bulk_delete": {"why": "삭제 대상 56건 ≥ 임계 50건", "count": 56, "actions": 2}}


def test_bulk_report_sets_hold_and_gate_pass_clears_it():
    st = AutoState({})
    hold = runner._update_bulk_hold(st, "W", BULK, NOW, [])
    assert hold and hold["count"] == 56 and "56건" in hold["why"]
    assert runner._bulk_holds(st, ["A", "W"]) == ["W"]
    runner._update_bulk_hold(st, "W", {"outcome": "ok"}, NOW, [])
    assert runner._bulk_holds(st, ["W"]) == []


def test_own_gate_passing_run_releases_the_hold_and_cadence():
    """사람이 휴지통에서 되살려 다음 실행이 게이트를 지나면 — 주기도 바로 x1."""
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    st.set_backoff_mult("W", 8)
    st.profile("W")["fail_streak"] = 7
    runner._update_bulk_hold(st, "W", {"outcome": "ok"}, NOW, [])
    assert runner._bulk_holds(st, ["W"]) == []
    assert st.backoff_mult("W") == 1.0 and st.profile("W")["fail_streak"] == 0


def test_gate_pass_without_a_hold_leaves_the_cadence_alone():
    st = AutoState({})
    st.set_backoff_mult("W", 4)
    runner._update_bulk_hold(st, "W", {"outcome": "ok"}, NOW, [])
    assert st.backoff_mult("W") == 4


@pytest.mark.parametrize("report", [
    {}, {"fault": "network_offline"}, {"outcome": "aborted_plan_changed"},
])
def test_unknown_outcomes_do_not_clear_the_hold(report):
    """게이트까지 못 간 실패는 보류가 풀렸는지 모른다 — 지우지 않는다."""
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    runner._update_bulk_hold(st, "W", report, NOW, [])
    assert runner._bulk_holds(st, ["W"]) == ["W"]


def test_dry_run_loop_never_sets_a_hold():
    st = AutoState({})
    assert runner._update_bulk_hold(st, "W", BULK, NOW, ["--dry-run"]) is None
    assert runner._bulk_holds(st, ["W"]) == []


def test_hold_falls_back_to_the_error_first_line():
    st = AutoState({})
    rep = {"outcome": "aborted_bulk_delete", "error": "대량 삭제로 판단해 중단: 80건\n둘째"}
    assert runner._update_bulk_hold(st, "W", rep, NOW, [])["why"].endswith("80건")


def test_bulk_notice_says_where_to_press_not_what_to_type():
    st = AutoState({})
    runner._note_outcome(st, "W", "held", "보류", BULK, NOW)
    msg = notify.load()[0]["message"]
    assert "y" in msg and "synchere.bat" in msg and "56건" in msg
    assert "--allow-bulk-delete" not in msg


def _tick_env(monkeypatch, report: dict, rc: int = 1):
    monkeypatch.setattr(runner, "load_auto", lambda: {
        "tick_sec": 120, "min_interval_sec": 1800, "day_gap_hours": 4, "work_hours": 9,
        "max_auto_deletes": 20, "max_auto_delete_mb": 200})
    monkeypatch.setattr(runner, "auto_profiles", lambda only=None: ["W"])
    monkeypatch.setattr(runner, "_reconcile", lambda st, names: None)
    monkeypatch.setattr(runner, "_last_sync_map", lambda names: {"W": None})
    monkeypatch.setattr(runner, "_now", lambda: NOW)
    monkeypatch.setattr(runner, "decide",
                        lambda *a, **k: runner.Decision("due", ["W"], "가장 오래 밀린 프로파일"))
    calls = []
    monkeypatch.setattr(runner, "_run_child", lambda name, auto, extra: calls.append(name) or rc)
    monkeypatch.setattr(runner, "_read_report", lambda name: dict(report))
    drained = []
    monkeypatch.setattr(keys, "drain", lambda backend=None: drained.append(1) or "")
    return calls, drained


def test_tick_announces_the_hold_and_how_to_open_it(monkeypatch, capsys):
    _calls, drained = _tick_env(monkeypatch, BULK)
    st = AutoState({})
    runner._tick(st, [], None)
    out = capsys.readouterr().out
    assert "y 를 누르면" in out and "56건" in out
    assert "폴더에서 직접 실행해" not in out
    assert runner._bulk_holds(st, ["W"]) == ["W"]
    assert drained, "알리기 전에 눌린 키를 버리지 않았다"


def test_hold_is_stamped_when_the_child_finished(monkeypatch):
    """스윕에서 앞 프로파일이 도는 사이 손으로 동기화한 기록이, 그 뒤에 세운 보류를
    '이미 풀렸다'로 지우면 안 된다 — 보류 시각은 틱 시작이 아니라 실행이 끝난 때."""
    _tick_env(monkeypatch, BULK)
    later = NOW + runner._dt.timedelta(minutes=10)
    clock = iter([NOW, later, later, later, later])
    monkeypatch.setattr(runner, "_now", lambda: next(clock, later))
    st = AutoState({})
    runner._tick(st, [], None)
    assert runner._parse(runner._bulk_hold(st, "W")["at"]) == later
    runner._settle_bulk_holds(st, {"W": NOW + runner._dt.timedelta(minutes=3)})
    assert runner._bulk_holds(st, ["W"]) == ["W"]


def test_dry_run_loop_never_advertises_y(monkeypatch, capsys):
    _tick_env(monkeypatch, BULK)
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    monkeypatch.setattr(runner, "decide", lambda *a, **k: runner.Decision("idle", [], ""))
    runner._tick(st, ["--dry-run"], None)
    runner._end_idle()
    assert "삭제 보류" not in capsys.readouterr().out


def test_tick_regular_child_keeps_unattended_guards(monkeypatch):
    """평소 틱의 자식 명령은 그대로다 — 무인 인자가 빠지는 경로는 없다."""
    cmd = runner._child_cmd("W", {"max_auto_deletes": 20, "max_auto_delete_mb": 200}, [])
    assert "--unattended" in cmd and "--max-auto-deletes" in cmd
    assert "--allow-bulk-delete" not in cmd


def test_idle_line_shows_the_pending_hold(monkeypatch, capsys):
    _tick_env(monkeypatch, BULK)
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    monkeypatch.setattr(runner, "decide", lambda *a, **k: runner.Decision("idle", [], ""))
    runner._tick(st, [], None)
    runner._end_idle()
    out = capsys.readouterr().out
    assert "| 삭제 보류 1개: y" in out
    worst = "[10:56:41] 대기 중  다음 동기화 11:20 · 퇴근 스윕 18:15 | 삭제 보류 9개: y"
    assert runner._display_width(worst) <= 78                 # 80칸 CP949 콘솔에서도 안 접힌다


def _wait(monkeypatch, st, typed: list[str], *, stop=lambda: False, seconds=10):
    handled = []
    monkeypatch.setattr(runner, "auto_profiles", lambda only=None: ["W"])
    monkeypatch.setattr(runner, "load_auto", lambda: {})
    monkeypatch.setattr(runner, "_handle_bulk_holds",
                        lambda st_, auto, names, extra: handled.append(names))
    feed = list(typed)
    reads = []

    def _drain(backend=None):
        reads.append(1)
        return feed.pop(0) if feed else ""

    monkeypatch.setattr(keys, "drain", _drain)
    clock = _Clock()

    def _sleep(sec):
        clock.t += sec

    runner._idle_wait(st, seconds, should_stop=stop, only=None, extra=[],
                      clock=clock, sleep=_sleep)
    return handled, reads


def test_idle_wait_ignores_keys_when_nothing_is_held(monkeypatch):
    handled, reads = _wait(monkeypatch, AutoState({}), ["y"])
    assert handled == [] and reads == [], "보류가 없는데 키를 읽었다"


@pytest.mark.parametrize("typed", [["", "abc", "y"], ["ㅛ"], ["xY"]])
def test_idle_wait_opens_the_offer_on_y(monkeypatch, typed):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    handled, _ = _wait(monkeypatch, st, typed)
    assert handled == [["W"]]


def test_idle_wait_other_keys_do_nothing(monkeypatch):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    handled, _ = _wait(monkeypatch, st, ["n", "\r", " "], seconds=3)
    assert handled == []


def test_idle_wait_honours_stop(monkeypatch):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    handled, reads = _wait(monkeypatch, st, ["y"], stop=lambda: True)
    assert handled == [] and reads == []


class _StubSH:
    BULK_DONE, BULK_DECLINED, BULK_NOTHING = "done", "declined", "nothing"
    BULK_PREVIEW_FAILED, BULK_CHANGED, BULK_FAILED = "preview_failed", "changed", "failed"

    def __init__(self, results: list[str], answers: list[str] | None = None) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def bulk_delete_flow(self, name, *, run, ask, base=(), report_path=""):
        self.calls.append({"name": name, "base": list(base), "report": report_path})
        ask("q? ")
        return self.results.pop(0), 0


def _handle(monkeypatch, st, names, results, answers):
    stub = _StubSH(results)
    monkeypatch.setattr(runner, "_sync_here", lambda: stub)
    feed = list(answers)
    monkeypatch.setattr(keys, "ask", lambda q, **kw: feed.pop(0) if feed else "no")
    monkeypatch.setattr(runner, "_read_report", lambda name: {"outcome": "ok", "fault": "ok"})
    monkeypatch.setattr(runner, "_write_status", lambda *a, **k: None)
    runner._handle_bulk_holds(st, {}, names, [])
    return stub


def test_handle_done_clears_hold_backoff_and_notice(monkeypatch):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    st.set_backoff_mult("W", 8)
    st.profile("W")["fail_streak"] = 241
    notify.add("W", "held", "대량 삭제 보류")
    stub = _handle(monkeypatch, st, ["W"], ["done"], ["yes"])
    assert runner._bulk_holds(st, ["W"]) == []
    assert st.backoff_mult("W") == 1.0 and st.profile("W")["fail_streak"] == 0
    assert not [i for i in notify.load() if i["kind"] == "held"]
    call = stub.calls[0]
    assert call["base"][0] == "--unattended" and "--max-auto-deletes" not in call["base"]
    assert call["report"] == str(auto_dir() / "last" / "W.json")
    assert runner._OFFER_ACTIVE is False


def test_handle_declined_keeps_the_hold(monkeypatch, capsys):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    _handle(monkeypatch, st, ["W"], ["declined"], ["no"])
    assert runner._bulk_holds(st, ["W"]) == ["W"]
    assert "다시 묻습니다" in capsys.readouterr().out


def test_handle_nothing_clears_the_hold_and_resets_the_cadence(monkeypatch):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    st.set_backoff_mult("W", 8)
    st.profile("W")["fail_streak"] = 7
    _handle(monkeypatch, st, ["W"], ["nothing"], ["no"])
    assert runner._bulk_holds(st, ["W"]) == []
    assert st.backoff_mult("W") == 1.0 and st.profile("W")["fail_streak"] == 0


def test_hold_resolved_elsewhere_is_settled_by_last_sync():
    """그 폴더의 synchere.bat 에서 지웠으면 게이트를 지난 실행이 last_sync_at 을 남긴다."""
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    st.set_backoff_mult("W", 8)
    notify.add("W", "held", "대량 삭제 보류")
    runner._settle_bulk_holds(st, {"W": NOW - runner._dt.timedelta(minutes=5)})
    assert runner._bulk_holds(st, ["W"]) == ["W"]              # 보류보다 오래된 동기화
    runner._settle_bulk_holds(st, {"W": None})
    assert runner._bulk_holds(st, ["W"]) == ["W"]
    runner._settle_bulk_holds(st, {"W": NOW + runner._dt.timedelta(minutes=1)})
    assert runner._bulk_holds(st, ["W"]) == []
    assert st.backoff_mult("W") == 1.0
    assert not [i for i in notify.load() if i["kind"] == "held"]


def test_y_while_config_is_locked_does_not_kill_the_loop(monkeypatch, capsys):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])
    monkeypatch.setattr(runner, "auto_profiles", lambda only=None: ["W"])

    def _locked():
        raise RuntimeError("설정 파일을 읽을 수 없습니다")

    monkeypatch.setattr(runner, "load_auto", _locked)
    monkeypatch.setattr(keys, "drain", lambda backend=None: "y")
    clock = _Clock()
    runner._idle_wait(st, 5, should_stop=lambda: False, only=None, extra=[],
                      clock=clock, sleep=lambda s: setattr(clock, "t", clock.t + s))
    assert "시작하지 못했습니다" in capsys.readouterr().out
    assert runner._bulk_holds(st, ["W"]) == ["W"]


@pytest.mark.parametrize("first", ["timeout", "cancel", "unavailable"])
def test_handle_stops_asking_when_nobody_answers(monkeypatch, first):
    """자리에 없는 사람에게 남은 보류를 하나씩 5분씩 붙잡지 않는다."""
    st = AutoState({})
    for n in ("A", "B"):
        runner._update_bulk_hold(st, n, BULK, NOW, [])
    stub = _handle(monkeypatch, st, ["A", "B"], ["declined", "declined"], [first, "yes"])
    assert [c["name"] for c in stub.calls] == ["A"]


def test_handle_declining_one_still_asks_the_next(monkeypatch):
    st = AutoState({})
    for n in ("A", "B"):
        runner._update_bulk_hold(st, n, BULK, NOW, [])
    stub = _handle(monkeypatch, st, ["A", "B"], ["declined", "done"], ["no", "yes"])
    assert [c["name"] for c in stub.calls] == ["A", "B"]
    assert runner._bulk_holds(st, ["A", "B"]) == ["A"]


def test_handle_survives_a_crashing_flow(monkeypatch, capsys):
    st = AutoState({})
    runner._update_bulk_hold(st, "W", BULK, NOW, [])

    class _Boom(_StubSH):
        def bulk_delete_flow(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_sync_here", lambda: _Boom([]))
    monkeypatch.setattr(runner, "_write_status", lambda *a, **k: None)
    runner._handle_bulk_holds(st, {}, ["W"], [])           # 예외가 밖으로 나오면 실패
    assert runner._bulk_holds(st, ["W"]) == ["W"]
    assert runner._OFFER_ACTIVE is False
    assert "boom" in capsys.readouterr().out


def test_ctrl_c_while_asking_is_no_not_stop(monkeypatch):
    monkeypatch.setattr(runner, "_OFFER_ACTIVE", True)
    monkeypatch.setattr(runner, "_OFFER_CANCEL", False)
    assert runner._sigint_should_stop() is False and runner._OFFER_CANCEL is True
    monkeypatch.setattr(runner, "_OFFER_ACTIVE", False)
    assert runner._sigint_should_stop() is True


def test_runner_uses_the_real_shared_flow():
    """러너가 쓰는 흐름은 사람 창과 같은 함수다 — 둘로 갈라지지 않았는지."""
    sh = runner._sync_here()
    assert sh is not None and callable(getattr(sh, "bulk_delete_flow", None))
    assert sh.BULK_DONE == "done"


def test_idle_line_padding_counts_hangul_as_two_columns(capsys):
    assert runner._display_width("·") == 2 and runner._display_width("가a") == 3
    runner._print_idle("[10:56:41] 대기 중  다음 동기화 11:20 | 삭제 보류 1개: y")
    runner._end_idle()
    line = capsys.readouterr().out.strip("\r\n")
    assert runner._display_width(line.rstrip()) <= 78
    assert runner._display_width(line) == 78
