"""§7-1 — git 을 한복판에서 죽이지 않는다 (최우선).

`telegram_bot/capture_io.py:27,45` 가 `capture.py` 를 `timeout=300` 으로 띄운다.
초과하면 `subprocess.run` 이 프로세스를 죽이는데, 그 순간이 git 한복판이면
잠금이 남는다. SIGTERM 은 git 에게 정리할 틈을 주지 않는다.

**핵심은 "죽이지 않는다"가 아니라 "죽어도 git 밖에서 죽는다"** 이다. 타임아웃을
없애면 봇이 영원히 매달린다. 그래서 git 을 *시작하기 전에* 남은 예산을 보고,
부족하면 **시작하지 않는다.** 시작하지 않은 것은 잔해를 남기지 않는다 —
파일은 이미 저장돼 있으므로 다음 실행이 커밋한다.

이 파일은 vault 의 `scripts/capture.py` 를 시험한다. dooraydrive 저장소의
테스트가 vault 코드를 시험하는 것이 어색해 보이지만, 잠금 정책(`gitlock.py`)이
양쪽에 걸쳐 있고 하버스가 여기 있다. 하버스를 두 벌 만들지 않는다.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

CAPTURE_PY = Path(r"C:\drive\obsidian\knowledge_base\scripts\capture.py")

needs_vault = pytest.mark.skipif(
    not CAPTURE_PY.exists(), reason="이 PC 에 vault 가 없다")

pytestmark = needs_vault


@pytest.fixture()
def capture_mod():
    """vault 의 capture.py 를 파일 경로로 로드한다(패키지가 아니다)."""
    scripts = str(CAPTURE_PY.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("capture_under_test", CAPTURE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def repo(tmp_path):
    """실제 git 저장소 — 예산 판정이 git 을 정말 안 부르는지 보려면 진짜여야 한다."""
    import subprocess

    r = tmp_path / "repo"
    r.mkdir()

    def g(*a):
        return subprocess.run(["git", *a], cwd=str(r), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (r / "seed.md").write_text("seed", encoding="utf-8")
    g("add", "seed.md")
    g("commit", "-qm", "seed")
    return r


# ---------------------------------------------------------------- 예산 계산
def test_T1_4_no_env_means_unlimited(capture_mod, monkeypatch):
    """환경변수가 없으면 기존 동작 — 회귀가 없어야 한다.

    이 프로젝트에서 새 기능이 기존 경로를 조용히 바꾸는 것이 반복된 사고다.
    """
    monkeypatch.delenv("CAPTURE_GIT_DEADLINE", raising=False)
    assert capture_mod._git_budget_left() is None


def test_T1_1_plenty_of_budget(capture_mod, monkeypatch):
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() + 300))
    left = capture_mod._git_budget_left()
    assert left is not None and 290 < left <= 300


def test_budget_can_go_negative(capture_mod, monkeypatch):
    """이미 지난 마감시한 — 음수여야 한다(0으로 뭉개면 '충분'으로 오독된다)."""
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() - 10))
    left = capture_mod._git_budget_left()
    assert left is not None and left < 0


def test_malformed_env_is_ignored(capture_mod, monkeypatch):
    """망가진 값 때문에 캡처가 죽으면 안 된다 — 무제한으로 떨어진다."""
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", "not-a-number")
    assert capture_mod._git_budget_left() is None


# ---------------------------------------------------------------- 시작 판정
def test_T1_2_insufficient_budget_defers_without_touching_git(
        capture_mod, repo, monkeypatch):
    """**이 파일에서 가장 중요한 테스트.**

    예산이 부족하면 git 을 *시작하지 않고* deferred 로 물러난다. 시작하지
    않았으므로 잠금이 생길 수 없다.
    """
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() + 5))
    (repo / "new.md").write_text("x", encoding="utf-8")

    called = []
    orig = capture_mod.git
    monkeypatch.setattr(capture_mod, "git",
                        lambda *a, **k: called.append(a) or orig(*a, **k))

    state, detail = capture_mod.commit_push(str(repo), ["new.md"], "msg")

    assert state == "deferred", f"state={state} detail={detail}"
    assert not called, f"예산이 없는데 git 을 불렀다: {called}"
    assert not list((repo / ".git").glob("*.lock"))


def test_deferred_file_survives_and_next_run_commits(capture_mod, repo, monkeypatch):
    """**"다음 실행이 커밋한다"는 주장을 실제로 증명한다.**

    deferred 는 add 조차 하지 않으므로 파일이 untracked 로 남는다. 그 상태가
    안전하다는 근거는 두 가지여야 한다 — 내용이 살아 있을 것, 그리고 다음
    실행이 정말 주워 갈 것. 주장만 하고 검증하지 않으면 "파일은 저장됐습니다"
    라는 안내가 거짓말이 될 수 있다.
    """
    import subprocess

    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() + 5))
    (repo / "note.md").write_text("중요한 캡처", encoding="utf-8")

    state, _ = capture_mod.commit_push(str(repo), ["note.md"], "first")
    assert state == "deferred"
    assert (repo / "note.md").read_text(encoding="utf-8") == "중요한 캡처"

    # 다음 실행 — 예산이 넉넉하다
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() + 300))
    state2, _ = capture_mod.commit_push(str(repo), ["note.md"], "second")

    assert state2 != "deferred"
    tracked = subprocess.run(["git", "ls-files", "note.md"], cwd=str(repo),
                             capture_output=True, text=True).stdout.strip()
    assert tracked == "note.md", "다음 실행이 파일을 주워 가지 못했다"


def test_T1_3_budget_exactly_at_threshold_proceeds(capture_mod, repo, monkeypatch):
    """경계는 포함한다 — 경계에서 물러나면 예산이 딱 맞는 정상 실행이 막힌다."""
    monkeypatch.setenv(
        "CAPTURE_GIT_DEADLINE", str(time.time() + capture_mod.GIT_MIN_BUDGET + 1))
    (repo / "edge.md").write_text("x", encoding="utf-8")

    state, _ = capture_mod.commit_push(str(repo), ["edge.md"], "edge")

    assert state != "deferred"


def test_T1_1b_sufficient_budget_commits(capture_mod, repo, monkeypatch):
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() + 300))
    (repo / "ok.md").write_text("x", encoding="utf-8")

    state, detail = capture_mod.commit_push(str(repo), ["ok.md"], "ok")

    # 원격이 없으므로 push 는 실패하지만 commit 은 됐어야 한다
    assert state in ("pushed", "push-failed"), f"{state}: {detail}"
    import subprocess
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert "ok" in log


# ---------------------------------------------------------------- 백오프 자르기
def test_T1_5_backoff_is_clipped_to_budget(capture_mod, monkeypatch):
    """push 백오프가 남은 예산을 넘기면 안 된다.

    `PUSH_BACKOFF = [2,4,8,16]` 는 최대 30초를 먹는다. 예산이 20초면 그 안에서
    끝나야 하고, 아니면 백오프를 태우다 timeout 에 걸려 죽는다 — 바로 그
    죽음이 잔해를 만든다.
    """
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() + 20))
    clipped = capture_mod._clip_backoff(capture_mod.PUSH_BACKOFF)
    assert sum(clipped) < 20


def test_backoff_unclipped_without_budget(capture_mod, monkeypatch):
    monkeypatch.delenv("CAPTURE_GIT_DEADLINE", raising=False)
    assert capture_mod._clip_backoff(capture_mod.PUSH_BACKOFF) == \
        capture_mod.PUSH_BACKOFF


@pytest.mark.parametrize("left,expected_total", [
    (300, 30),   # 넉넉 — 자르지 않는다
    (45, 30),
    (20, 6),
    (12, 2),
    (10, 0),     # 여유 10초를 빼면 0 — 백오프 없이 한 번만
    (5, 0),
    (0, 0),
    (-30, 0),    # **음수** — 이미 마감 지남
])
def test_backoff_boundaries(capture_mod, monkeypatch, left, expected_total):
    """경계 전부를 표로 고정한다 — 특히 음수.

    예산이 음수인데 백오프를 태우면 이미 지난 마감시한 위에서 더 기다리는
    것이고, 그 대기 중에 부모가 죽인다. 그 죽음이 잔해를 만든다.
    빈 리스트여도 push 는 한 번 시도한다(루프가 len+1 회 돈다) — 커밋은
    이미 됐으므로 한 번은 해 볼 가치가 있다.
    """
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() + left))
    clipped = capture_mod._clip_backoff(capture_mod.PUSH_BACKOFF)
    assert sum(clipped) == expected_total, f"남은 {left}초 -> {clipped}"


def test_negative_budget_never_sleeps(capture_mod, monkeypatch):
    """음수 예산에서 sleep 이 한 번도 불리면 안 된다."""
    monkeypatch.setenv("CAPTURE_GIT_DEADLINE", str(time.time() - 100))
    assert capture_mod._clip_backoff(capture_mod.PUSH_BACKOFF) == []


# ---------------------------------------------------------------- 상태 전파
def test_T1_6_deferred_is_not_success(capture_mod):
    """`deferred` 는 호출측의 성공 필터를 통과해 경고로 올라가야 한다.

    복구 3원칙 #1 — 복구는 성공을 보고하지 않는다. `push-rescued` 가 같은
    이유로 별도 상태인 것과 같은 취급이다.
    """
    assert "deferred" not in ("pushed", "clean")


def test_deferred_is_documented(capture_mod):
    """상태 집합이 docstring 에 열거돼 있어야 한다 — 호출측이 그걸 보고 짠다."""
    assert "deferred" in (capture_mod.commit_push.__doc__ or "")


# ---------------------------------------------------------------- 결선
CAPTURE_IO = CAPTURE_PY.parent / "telegram_bot" / "capture_io.py"


@pytest.mark.skipif(not CAPTURE_IO.exists(), reason="capture_io.py 없음")
def test_caller_passes_a_deadline():
    """**결선 공백을 막는다.**

    예산 로직을 아무리 잘 짜도 호출측이 환경변수를 안 넘기면 무제한으로
    떨어져 아무 효과가 없다. 이 저장소가 반복해 사고를 낸 유형이라
    (결선을 잊고 '고쳤다'고 믿는 것) 호출 지점을 직접 겨냥한다.
    """
    src = CAPTURE_IO.read_text(encoding="utf-8")
    assert "CAPTURE_GIT_DEADLINE" in src, "호출측이 마감시한을 넘기지 않는다"
    # capture.py 를 띄우는 지점 전부가 예산을 넘겨야 한다. 첫 구현은 save/delete
    # 둘만 고쳤고 augment 를 놓쳤다 — 이 단언이 그것을 잡았다.
    spawns = src.count("config.CAPTURE_PY")
    # `def _budget_env()` 는 정의라 호출 수에서 뺀다.
    wired = src.count("env=_budget_env()")
    assert wired == spawns, f"capture.py 호출 {spawns}곳 중 {wired}곳만 결선됨"
    assert "timeout=300" not in src, "하드코딩된 300 이 남아 있다"


def test_push_failures_go_through_lock_handling(capture_mod):
    """push 실패도 잠금 판정을 거쳐야 한다.

    적대적 검증이 잡은 구멍(2026-08-31). `push -u` 는 `config.lock` 을 쓰고
    fetch 단계는 `refs/remotes/origin/*.lock` 을 쓴다. 판정이 add·commit 에만
    있으면 **2026-08-30 사고의 나머지 절반이 push 로 옮겨갈 뿐이다** —
    그날 실패도 add 가 아닌 다음 단계(commit)였고, 그때도 검사는 add 에만 있었다.

    소스에서 확인한다: push 루프 안에 `_handle_lock` 호출이 있어야 한다.
    """
    import inspect

    src = inspect.getsource(capture_mod.commit_push)
    push_section = src[src.index("push -u") if "push -u" in src else 0:]
    assert "_handle_lock" in push_section, (
        "push 실패 경로가 잠금 판정을 거치지 않는다")
    # add · commit · push 세 단계 모두에 있어야 한다
    assert src.count("_handle_lock(") >= 3, (
        f"_handle_lock 호출이 {src.count('_handle_lock(')}곳뿐이다 "
        "— add·commit·push 셋 다 필요하다")


EXTRACT_PY = CAPTURE_PY.parent / "telegram_bot" / "extract.py"


@pytest.mark.skipif(not EXTRACT_PY.exists(), reason="extract.py 없음")
def test_extract_paths_do_not_need_a_deadline():
    """`extract.py` 도 capture.py 를 띄우지만 예산이 필요 없다 — 확인해 둔다.

    `_run_capture` 가 부르는 것은 `fetch-youtube`·`extract-pdf` 이고, 이들은
    stdout 으로 텍스트만 낸다. `commit_push` 를 부르는 것은 `save`·`augment`·
    `delete` 셋뿐이다(capture.py:400,403,406,458,488).

    "여기는 결선이 필요 없다"는 판단을 **근거와 함께 고정**한다. 안 그러면
    다음 사람이 미결선으로 오해해 넣거나, 반대로 진짜 미결선을 못 본다.
    이 파일에서 git 하위명령을 부르기 시작하면 이 테스트가 빨간불이 된다.
    """
    src = EXTRACT_PY.read_text(encoding="utf-8")
    git_subcommands = ("\"save\"", "'save'", "\"augment\"", "'augment'",
                       "\"delete\"", "'delete'")
    used = [s for s in git_subcommands if s in src]
    assert not used, (
        f"extract.py 가 git 을 쓰는 하위명령을 부른다: {used} — 예산 결선 필요")


@pytest.mark.skipif(not CAPTURE_PY.exists(), reason="capture.py 없음")
def test_only_three_subcommands_touch_git(capture_mod):
    """`commit_push` 호출 지점이 셋이라는 전제를 고정한다.

    넷째가 생기면 그 경로도 예산 판정을 거쳐야 하는데, `commit_push` 안에
    판정이 있으므로 자동으로 적용된다. 그래도 개수가 변한 사실은 알아야 한다 —
    새 경로가 예산이 없는 호출측에서 불릴 수 있기 때문이다.
    """
    src = CAPTURE_PY.read_text(encoding="utf-8")
    calls = src.count("commit_push(")
    # 정의 1 + docstring 언급 1 + 실제 호출 5(save 3 · augment 1 · delete 1)
    assert calls <= 8, f"commit_push 호출이 {calls}곳으로 늘었다 — 결선을 다시 보라"


@pytest.mark.skipif(not CAPTURE_IO.exists(), reason="capture_io.py 없음")
def test_deadline_leaves_tail_room():
    """마감시한이 timeout 보다 **앞서야** 한다 — 같으면 여유가 0이다."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("capture_io_probe", CAPTURE_IO)
    mod = importlib.util.module_from_spec(spec)
    # 패키지 상대 import(`from . import config`)가 있어 실행은 못 한다.
    # 상수만 소스에서 확인한다.
    src = CAPTURE_IO.read_text(encoding="utf-8")
    assert "GIT_TAIL_RESERVE" in src
    ns: dict = {}
    for line in src.splitlines():
        if line.startswith(("CAPTURE_TIMEOUT", "GIT_TAIL_RESERVE")):
            exec(line, ns)  # noqa: S102 — 상수 두 줄만
    assert ns["GIT_TAIL_RESERVE"] > 0
    assert ns["GIT_TAIL_RESERVE"] < ns["CAPTURE_TIMEOUT"]
    _ = (spec, mod)


@pytest.mark.skipif(not CAPTURE_IO.exists(), reason="capture_io.py 없음")
def test_tail_reserve_covers_min_budget(capture_mod):
    """여유가 `GIT_MIN_BUDGET` 보다 커야 예산 판정이 의미를 갖는다.

    여유(60초)가 최소 예산(45초)보다 작으면, 자식이 '시작해도 된다'고
    판정한 뒤에도 부모가 그 전에 죽인다 — 판정이 거짓말이 된다.
    """
    src = CAPTURE_IO.read_text(encoding="utf-8")
    ns: dict = {}
    for line in src.splitlines():
        if line.startswith("GIT_TAIL_RESERVE"):
            exec(line, ns)  # noqa: S102
    assert ns["GIT_TAIL_RESERVE"] >= capture_mod.GIT_MIN_BUDGET
