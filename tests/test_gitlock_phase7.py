"""§7 — 잠금이 *생기는* 원인을 줄인다.

앞 단계는 잠금이 생긴 뒤의 처리를 고쳤다. 여기는 그 앞이다.

방향을 정한 실측 하나: **git이 잠금을 쥐는 구간은 수 밀리초다.** 정상 종료하는
git 은 잔해를 남기지 않는다(잔해 10건이 전부 0바이트였던 것과 일치). 따라서
잔해의 원인은 경합이 아니라 **중간에 죽는 것**이다. 경합은 뒤에 온 쪽이 실패할
뿐 잔해를 안 남긴다.

그래서 §7 의 우선순위가 계획 원문과 다르다 — 뮤텍스보다 '죽음을 막는 것'이 먼저다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.gitlock_harness import scenario as sc

VAULT = Path(r"C:\drive\obsidian")
BOOTSTRAP_CMD = VAULT / "agent_base" / "deploy" / "bootstrap.cmd"
SYNCHERE_BAT = VAULT / "knowledge_base" / "git+dooray-synchere.bat"

needs_vault = pytest.mark.skipif(
    not VAULT.exists(), reason="이 PC 에 vault 가 없다(데스크톱 구성)")


@pytest.fixture()
def scn(tmp_path):
    s = sc.build(tmp_path, with_remote=True, with_second_pc=True)
    yield s
    s.release_all()


# ---------------------------------------------------------------- T2. pause
@needs_vault
def test_T2_1_bootstrap_cmd_pauses_before_exit():
    """`bootstrap.cmd` 가 창을 즉시 닫으면 안 된다.

    `bootstrap.ps1` 은 4개 저장소에 add·commit·config 를 돈다(B7~B11).
    창이 바로 닫히면 사람이 "끝난 줄 알고" 그 중간을 끊는다 — 그것이 잠금
    잔해를 만드는 경로다. 성공이든 실패든 결과를 보고 닫게 한다.
    """
    text = BOOTSTRAP_CMD.read_text(encoding="ascii", errors="replace")
    assert "pause" in text.lower(), "bootstrap.cmd 에 pause 가 없다 — 창이 즉시 닫힌다"


@needs_vault
def test_T2_2_bootstrap_cmd_is_ascii_only():
    """cmd.exe 는 BOM·비ASCII 를 콘솔 코드페이지로 오독한다(전역 인코딩 교훈)."""
    raw = BOOTSTRAP_CMD.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM 이 있다"
    bad = [b for b in raw if b > 0x7F]
    assert not bad, f"비ASCII 바이트 {len(bad)}개"


@needs_vault
def test_T2_2b_batch_files_are_crlf():
    r"""`.cmd`·`.bat` 은 CRLF 여야 한다.

    vault 의 `.gitattributes:7-9` 가 `*.cmd text eol=crlf` 를 강제하고 이유까지
    적어 뒀다 — *"cmd.exe 는 LF 배치 파일에서 라벨·goto·복합 구문이 어긋나고,
    **실패가 조용하다**"*.

    실측 2026-08-30: `bootstrap.cmd` 를 고쳐 쓰면서 LF 로 저장했고
    (CRLF 0 / LF 19), git 이 계속 dirty 로 봤다. 편집 도구가 LF 로 쓰는 것이
    기본이라 **이 파일을 손댈 때마다 재발한다.** goto 가 어긋나도 조용하니
    사람이 알아채지 못한다.
    """
    raw = BOOTSTRAP_CMD.read_bytes()
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    assert crlf > 0, "CRLF 가 하나도 없다 — cmd.exe 가 조용히 깨진다"
    assert lone_lf == 0, f"단독 LF {lone_lf}개 — .gitattributes 규칙 위반"


@needs_vault
def test_T2_3_synchere_wrapper_delegates_its_pause():
    """계획 원문의 지적을 정정한 근거를 고정한다.

    `git+dooray-synchere.bat` 은 pause 가 없다고 적혀 있었으나, 성공 경로가
    `synchere.bat` 을 호출하고(:38) 그쪽이 pause 로 끝난다. 실패 경로에도
    pause 가 있다(:47). 따라서 이 파일은 고칠 대상이 아니다.

    이 테스트는 그 위임 구조가 유지되는지 지킨다 — synchere 호출이 사라지면
    진짜로 pause 가 없어진다.
    """
    text = SYNCHERE_BAT.read_text(encoding="ascii", errors="replace")
    assert "synchere.bat" in text, "성공 경로의 위임 대상이 사라졌다"
    assert "pause" in text.lower(), "실패 경로의 pause 가 사라졌다"


# ------------------------------------------------- T5. 비잠금 잔해는 건드리지 않는다
def test_T5_1_auto_merge_alone_is_not_a_lock_error(scn):
    """§8 실측: `AUTO_MERGE` 는 정상 커밋이 성공하면 git 이 스스로 치웠다.

    막고 있지 않은 것을 치우는 습관이 `merge --abort` 를 부르는 충동과 같은
    뿌리다. 잠금 오류로 분류하면 격리 대상이 되어 손을 대게 된다.
    """
    from tools import gitlock

    (scn.work / ".git" / "AUTO_MERGE").write_text("deadbeef\n", encoding="utf-8")
    state = gitlock.classify("error: something unrelated happened", scn.work)

    assert not state.is_lock_error
    assert (scn.work / ".git" / "AUTO_MERGE").exists(), "잔해를 건드렸다"


def test_T5_2_merge_head_is_never_touched(scn):
    """`MERGE_HEAD` 는 **진행 중인 사람의 작업**이다. 지우면 그 작업이 사라진다."""
    from tools import gitlock

    (scn.work / ".git" / "MERGE_HEAD").write_text("abc123\n", encoding="utf-8")
    scn.make_lock(sc.LockSpec(".git/index.lock", "dead-empty"))
    stderr = f"fatal: Unable to create '{scn.work}/.git/index.lock': File exists."

    gitlock.handle(stderr, scn.work)

    assert (scn.work / ".git" / "MERGE_HEAD").exists(), "MERGE_HEAD 를 지웠다"


def test_T5_3_only_the_lock_is_quarantined(scn):
    """잔해와 잠금이 함께 있어도 **잠금만** 격리한다."""
    from tools import gitlock

    (scn.work / ".git" / "AUTO_MERGE").write_text("x\n", encoding="utf-8")
    (scn.work / ".git" / "MERGE_MSG").write_text("y\n", encoding="utf-8")
    scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    stderr = ("fatal: cannot lock ref 'HEAD': Unable to create "
              f"'{scn.work}/.git/HEAD.lock': File exists.")

    result = gitlock.handle(stderr, scn.work)

    assert result["action"] == gitlock.Action.QUARANTINE
    assert len(result["quarantined"]) == 1
    assert (scn.work / ".git" / "AUTO_MERGE").exists()
    assert (scn.work / ".git" / "MERGE_MSG").exists()


# ------------------------------------------------- T4. 경합은 잔해를 남기지 않는다
def test_T4_concurrent_commits_leave_no_debris(scn):
    """§7-4(뮤텍스)를 **넣지 않기로 한 결정의 근거**를 검증한다.

    두 프로세스가 같은 저장소에 동시에 커밋하면 한쪽은 실패한다. 그러나
    **잔해는 남지 않는다** — 실패한 쪽의 git 이 정상 종료하며 자기 잠금을
    치우기 때문이다. 경합은 안전 문제가 아니라 편의 문제다.

    이 전제가 깨지면(잔해가 남으면) 뮤텍스를 다시 검토해야 한다.
    """
    procs = []
    for i in range(4):
        (scn.work / f"race{i}.md").write_text(str(i), encoding="utf-8")
    for i in range(4):
        procs.append(subprocess.Popen(
            ["git", "add", f"race{i}.md"], cwd=str(scn.work),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for p in procs:
        p.wait(timeout=60)

    leftovers = scn.loose_locks()
    assert not leftovers, f"경합이 잔해를 남겼다: {[p.name for p in leftovers]}"
