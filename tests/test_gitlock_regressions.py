"""회귀 시나리오 — 실제로 난 사고를 박제한다 (계획 §2.5 D층).

이것이 통과하지 못하면 어떤 개선도 채택하지 않는다.

**지금은 일부가 의도적으로 실패한다.** `tools/gitlock.py` 가 baseline —
`lib.ps1:110` 의 결함을 그대로 옮긴 상태 — 이기 때문이다. 개선의 출발점이
눈에 보여야 한다는 것이 autoresearch 의 방식이고, 실패하는 시나리오가
곧 할 일 목록이다.

`xfail(strict=True)` 로 표시한 것은 **지금 실패하는 것이 정상**이라는 뜻이다.
개선이 그것을 고치면 `XPASS` 로 빨간불이 뜨고, 그때 표시를 떼면서 진짜
회귀 테스트로 승격한다.
"""
from __future__ import annotations

import pytest

from tests.gitlock_harness import scenario as sc
from tests.gitlock_harness import score as sco

from tools import gitlock


@pytest.fixture()
def scn(tmp_path):
    s = sc.build(tmp_path, with_remote=True, with_second_pc=True)
    yield s
    s.release_all()


# ---------------------------------------------------------------- R8 (baseline 결함)
@pytest.mark.xfail(strict=True, reason=(
    "baseline: lib.ps1:110 의 index.lock 전용 정규식을 그대로 옮겼다. "
    "HEAD.lock 을 못 잡는 것이 현재 상태이고, 이것이 개선 대상 1번이다."))
def test_R8_head_lock_is_recognized(scn):
    """R8 — `index.lock` 재시도는 되는데 `HEAD.lock` 은 정규식에 안 걸린다.

    2026-08-30 실패가 정확히 이것이었다. git 이 낸 오류 문구를 그대로 쓴다.
    """
    scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    stderr = ("fatal: cannot lock ref 'HEAD': Unable to create "
              f"'{scn.work}/.git/HEAD.lock': File exists.")

    state = gitlock.classify(stderr, scn.work)

    assert state.is_lock_error, "HEAD.lock 을 잠금 오류로 인식하지 못했다"


@pytest.mark.xfail(strict=True, reason="baseline: refs/*.lock 도 같은 이유로 미포착")
def test_R8b_ref_lock_is_recognized(scn):
    scn.make_lock(sc.LockSpec(".git/refs/heads/main.lock", "dead-empty"))
    stderr = ("error: cannot lock ref 'refs/heads/main': Unable to create "
              f"'{scn.work}/.git/refs/heads/main.lock': File exists.")

    assert gitlock.classify(stderr, scn.work).is_lock_error


# ---------------------------------------------------------------- 지금도 통과해야 하는 것
def test_index_lock_is_recognized(scn):
    """baseline 이 유일하게 잡는 것. 이것마저 안 되면 완전히 고장이다."""
    scn.make_lock(sc.LockSpec(".git/index.lock", "dead-empty"))
    stderr = (f"fatal: Unable to create '{scn.work}/.git/index.lock': "
              "File exists.")

    state = gitlock.classify(stderr, scn.work)

    assert state.is_lock_error
    assert state.live is False          # 아무도 안 쥠
    assert state.empty is True


def test_live_holder_is_detected(scn):
    """살아있는 보유자는 기다려야 한다 — 절대 지우지 않는다."""
    scn.make_lock(sc.LockSpec(".git/index.lock", "live-nongit"))
    stderr = f"fatal: Unable to create '{scn.work}/.git/index.lock': File exists."

    state = gitlock.classify(stderr, scn.work)

    assert state.live is True, "배타로 잡힌 잠금을 죽은 잔해로 판정했다"
    assert gitlock.decide(state) == gitlock.Action.WAIT


def test_dead_remnant_is_quarantined_not_deleted(scn):
    """R1 계열 — 0바이트 잔해는 격리 후 진행."""
    scn.make_lock(sc.LockSpec(".git/index.lock", "dead-empty"))
    stderr = f"fatal: Unable to create '{scn.work}/.git/index.lock': File exists."

    result = gitlock.handle(stderr, scn.work)

    assert result["action"] == gitlock.Action.QUARANTINE
    assert result["quarantined"], "격리 경로가 비었다"
    moved = scn.root / "work" / "_to_delete" / "locks"
    assert list(moved.glob("*.quarantine")), "격리 폴더에 파일이 없다"
    assert not (scn.work / ".git" / "index.lock").exists()


def test_partial_write_remnant_stops(scn):
    """내용이 있는 잔해는 격리하되 **진행하지 않는다** — 무엇을 덮는지 모른다."""
    scn.make_lock(sc.LockSpec(".git/index.lock", "dead-partial", content="half"))
    stderr = f"fatal: Unable to create '{scn.work}/.git/index.lock': File exists."

    state = gitlock.classify(stderr, scn.work)

    assert state.empty is False
    assert gitlock.decide(state) == gitlock.Action.STOP


def test_non_lock_error_proceeds(scn):
    """잠금과 무관한 오류를 잠금으로 오인하면 안 된다."""
    state = gitlock.classify("error: pathspec 'nope' did not match", scn.work)
    assert not state.is_lock_error
    assert gitlock.decide(state) == gitlock.Action.PROCEED


def test_missing_lock_file_stops(scn):
    """오류는 났는데 파일이 없다 — 판정 불가는 진행이 아니라 멈춤."""
    stderr = f"fatal: Unable to create '{scn.work}/.git/index.lock': File exists."
    state = gitlock.classify(stderr, scn.work)
    assert state.live is None
    assert gitlock.decide(state) == gitlock.Action.STOP


# ---------------------------------------------------------------- 정적 검사
def test_gitlock_source_has_no_forbidden_commands():
    """개선 대상 자신이 금지 명령을 쓰면 안 된다 — 매 실험마다 도는 검사."""
    src = (gitlock.__file__ and __import__("pathlib").Path(gitlock.__file__)
           .read_text(encoding="utf-8"))
    assert sco.scan_forbidden(src) == [], "gitlock.py 가 금지 명령을 쓴다"


def test_quarantine_naming_is_stable(scn):
    """이름 규칙이 하나여야 한다 — 기존 잔해는 셋으로 갈려 재사용 불가였다."""
    p = scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    dest = gitlock.quarantine(p, scn.work)
    assert dest.name.startswith("HEAD.lock.")
    assert dest.name.endswith(".quarantine")


def test_quarantine_does_not_collide(scn):
    """같은 이름이 두 번 격리돼도 덮어쓰지 않는다."""
    first = scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    d1 = gitlock.quarantine(first, scn.work)
    second = scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    d2 = gitlock.quarantine(second, scn.work)
    assert d1 != d2 and d1.exists() and d2.exists()
