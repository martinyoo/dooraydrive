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

from pathlib import Path

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
def test_R8_head_lock_is_recognized(scn):
    """R8 — `index.lock` 재시도는 되는데 `HEAD.lock` 은 정규식에 안 걸렸다.

    2026-08-30 실패가 정확히 이것이었다. git 이 낸 오류 문구를 그대로 쓴다.
    baseline(이름 열거 방식)에서 실패했고, 경로 추출 방식으로 바꿔 통과한다.
    """
    scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    stderr = ("fatal: cannot lock ref 'HEAD': Unable to create "
              f"'{scn.work}/.git/HEAD.lock': File exists.")

    state = gitlock.classify(stderr, scn.work)

    assert state.is_lock_error, "HEAD.lock 을 잠금 오류로 인식하지 못했다"
    assert state.name == "HEAD.lock"
    assert state.live is False and state.empty is True


def test_R8b_ref_lock_is_recognized(scn):
    scn.make_lock(sc.LockSpec(".git/refs/heads/main.lock", "dead-empty"))
    stderr = ("fatal: cannot lock ref 'HEAD': Unable to create "
              f"'{scn.work}/.git/refs/heads/main.lock': File exists.")

    state = gitlock.classify(stderr, scn.work)
    assert state.is_lock_error and state.name == "main.lock"


def test_R8c_config_lock_is_recognized(scn):
    """config 만 문구 형태가 다르다 — 경로에 `.lock` 이 안 붙는다.

        error: could not lock config file .git/config: File exists

    이름 열거 방식이든 경로 추출 방식이든 이것만은 특수 처리가 필요하다.
    실측으로 확인한 유일한 예외라 테스트로 못 박는다.
    """
    scn.make_lock(sc.LockSpec(".git/config.lock", "dead-empty"))
    stderr = "error: could not lock config file .git/config: File exists"

    state = gitlock.classify(stderr, scn.work)

    assert state.is_lock_error, "config.lock 을 인식하지 못했다"
    assert state.name == "config.lock"


def test_unknown_lock_type_is_still_caught(scn):
    """**이름 목록을 쓰지 않는 것의 값** — 처음 보는 잠금도 잡힌다.

    git 이 새 잠금을 도입해도, 목록을 갱신하지 않아도 걸린다. 이것이
    lib.ps1:110 과 capture.py:133 이 함께 앓던 병의 구조적 해법이다.
    """
    scn.make_lock(sc.LockSpec(".git/shallow.lock", "dead-empty"))
    stderr = f"fatal: Unable to create '{scn.work}/.git/shallow.lock': File exists."

    state = gitlock.classify(stderr, scn.work)
    assert state.is_lock_error and state.name == "shallow.lock"


def test_R11_powershell51_wrapped_path(scn):
    """R11 — Windows PowerShell 5.1 이 stderr 를 콘솔 너비에서 접는다.

    2026-08-30 실측. lib.ps1 결선을 5.1 에서 시험하자 '판단 불가'로 떨어졌다.
    PowerShell 7 에서는 잘 됐다 — **5.1 에서만 조용히 실패**하는 종류다.
    lib.ps1:3 이 5.1 호환을 요구하므로 이것을 놓치면 실제 운용에서 죽는다.

    접힌 실제 출력을 그대로 쓴다.
    """
    scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    wrapped = (
        "fatal: cannot lock ref 'HEAD': Unable to create "
        f"'{scn.work}/.gi\nt/HEAD.lock': File exists.")

    state = gitlock.classify(wrapped, scn.work)

    assert state.is_lock_error, "접힌 줄 때문에 잠금을 인식하지 못했다"
    assert state.name == "HEAD.lock"
    assert state.live is False


def test_unwrapping_does_not_break_normal_output(scn):
    """되펴기가 멀쩡한 여러 줄 출력을 망가뜨리면 안 된다."""
    scn.make_lock(sc.LockSpec(".git/index.lock", "dead-empty"))
    normal = (f"fatal: Unable to create '{scn.work}/.git/index.lock': "
              "File exists.\n\nAnother git process seems to be running.")

    state = gitlock.classify(normal, scn.work)
    assert state.name == "index.lock"


def test_lock_error_without_path_stops(scn):
    """경로를 못 뽑으면 '잠금인 건 알지만 대상을 모른다' = 멈춤.

    모르는 채 진행하는 것이 이 프로젝트에서 반복된 사고의 형태다.
    """
    stderr = ("Another git process seems to be running in this repository. "
              "Please make sure all processes are terminated then try again.")

    state = gitlock.classify(stderr, scn.work)

    assert state.is_lock_error
    assert state.live is None
    assert gitlock.decide(state) == gitlock.Action.STOP


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
    moved = gitlock.quarantine_dir(scn.work)
    assert list(moved.glob("*.quarantine")), "격리 폴더에 파일이 없다"
    assert not (scn.work / ".git" / "index.lock").exists()


def test_R12_quarantine_stays_outside_the_repository(scn):
    """R12 — 격리가 저장소를 오염시키면 안 된다.

    실측 2026-08-31: 처음에는 `<repo>/_to_delete/locks/` 에 격리했다(사람이
    손으로 하던 선례를 따랐다). 그 경로가 git 추적 대상이라 격리한 잠금이
    **커밋되어 origin 으로 나갔다** — knowledge_base 에서 13건을 확인했다.
    `sync.ps1` 의 `add -A` 가 쓸어 담는다.

    그러면 그 파일들이 다른 PC 로 pull 되어 내려간다. **잔해가 PC 사이를
    옮겨 다니는 것을 막으려던 작업이 정확히 그 일을 하고 있었다.**
    """
    from tools import gitlock

    scn.make_lock(sc.LockSpec(".git/index.lock", "dead-empty"))
    stderr = f"fatal: Unable to create '{scn.work}/.git/index.lock': File exists."

    result = gitlock.handle(stderr, scn.work)

    assert result["quarantined"]
    for p in result["quarantined"]:
        moved = Path(p).resolve()
        assert not str(moved).startswith(str(scn.work.resolve())), (
            f"격리 파일이 저장소 안에 있다: {moved}")
    # 저장소 안에 새 파일이 생기지 않았는지도 확인 — untracked 도 안 된다
    assert not (scn.work / "_to_delete").exists()


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
