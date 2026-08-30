"""시나리오 생성기 자체를 검증한다 — 하버스가 만드는 상황이 진짜인가.

채점기와 `gitlock.py` 보다 먼저 이것이 서야 한다. 시나리오가 의도한 상황을
못 만들면 그 위의 모든 측정이 무의미하고, **그 고장은 조용하다** — 시나리오가
아무 잠금도 안 만들었는데 "잠금 처리 성공"으로 채점될 수 있다.

특히 `live-nongit` 이 **진짜로 git 을 막는지**가 핵심이다. 막지 못하면
"살아있는 보유자를 기다린다"는 규칙 전체가 검증되지 않은 채로 통과한다.
"""
from __future__ import annotations

import pytest

from tests.gitlock_harness import scenario as sc


@pytest.fixture()
def scn(tmp_path):
    s = sc.build(tmp_path, with_remote=True, with_second_pc=True)
    yield s
    s.release_all()


# ---------------------------------------------------------------- 기본 구성
def test_build_creates_working_repos(scn):
    assert (scn.origin / "HEAD").exists()          # bare
    assert (scn.work / ".git").is_dir()
    assert (scn.work2 / ".git").is_dir()
    assert sc.git("rev-parse", "HEAD", cwd=scn.work).ok


def test_work_and_origin_are_connected(scn):
    r = sc.git("rev-parse", "origin/main", cwd=scn.work)
    assert r.ok and r.out.strip()


def test_build_refuses_real_vault_paths(tmp_path, monkeypatch):
    """생성기가 볼트 안에 저장소를 만들려 하면 즉시 멈춘다."""
    import tests.gitlock_harness as h
    monkeypatch.setattr(h, "REAL_VAULT_ROOTS", (tmp_path,))
    with pytest.raises(AssertionError, match="실제 볼트"):
        sc.build(tmp_path)


# ---------------------------------------------------------------- 잠금 생성
def test_dead_empty_lock_is_zero_bytes_and_unheld(scn):
    p = scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    assert p.exists() and p.stat().st_size == 0
    # 아무도 안 쥐었으므로 배타 열기가 성공해야 한다
    with open(p, "r+b"):
        pass


def test_dead_partial_lock_has_content(scn):
    p = scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-partial", content="abc123"))
    assert p.stat().st_size > 0


def test_age_is_applied(scn):
    import time
    p = scn.make_lock(sc.LockSpec(".git/index.lock", "dead-empty", age_sec=3600))
    assert time.time() - p.stat().st_mtime > 3000


def test_live_nongit_lock_actually_blocks_git(scn):
    """**하버스에서 가장 중요한 검증.**

    배타로 붙잡은 index.lock 이 진짜로 git 을 막지 못하면, "살아있는 보유자를
    기다린다"는 규칙이 검증되지 않은 채 통과한다 — 시나리오는 만들었는데
    상황은 없는 상태다.
    """
    scn.make_lock(sc.LockSpec(".git/index.lock", "live-nongit"))
    (scn.work / "blocked.md").write_text("x", encoding="utf-8")

    r = sc.git("add", "--", "blocked.md", cwd=scn.work)

    assert not r.ok, "배타 잠금이 git add 를 막지 못했다 — 하버스가 고장"
    assert "index.lock" in r.text


def test_release_all_unblocks_git(scn):
    """놓으면 다시 되어야 한다 — 안 그러면 뒤 테스트가 오염된다."""
    scn.make_lock(sc.LockSpec(".git/index.lock", "live-nongit"))
    assert not sc.git("add", "--", "seed.md", cwd=scn.work).ok

    scn.release_all()
    (scn.work / ".git" / "index.lock").unlink()

    assert sc.git("add", "--", "seed.md", cwd=scn.work).ok


def test_head_lock_blocks_commit_not_add(scn):
    """L 축의 구분이 실재하는지 — HEAD.lock 은 add 가 아니라 ref 갱신을 막는다.

    2026-08-30 실패가 정확히 이 형태였다: add 는 됐고 commit 이 죽었다.
    """
    scn.make_lock(sc.LockSpec(".git/HEAD.lock", "dead-empty"))
    (scn.work / "x.md").write_text("x", encoding="utf-8")

    assert sc.git("add", "--", "x.md", cwd=scn.work).ok      # add 는 통과
    r = sc.git("commit", "-m", "blocked", cwd=scn.work)

    assert not r.ok
    assert "HEAD.lock" in r.text or "cannot lock ref" in r.text


# ---------------------------------------------------------------- 상태 만들기
def test_stage_leaves_staged_file(scn):
    scn.stage("staged.md", "content")
    r = sc.git("diff", "--cached", "--name-only", cwd=scn.work)
    assert "staged.md" in r.out


def test_diverge_makes_both_sides_ahead(scn):
    scn.diverge()
    r = sc.git("rev-list", "--left-right", "--count", "origin/main...HEAD",
               cwd=scn.work)
    behind, ahead = r.out.split()
    assert int(behind) >= 1 and int(ahead) >= 1


def test_hooks_do_not_hold_locks(scn):
    """실측 고정: 훅이 도는 동안 git 은 잠금을 쥐지 않는다.

    계획 §4.3의 초안은 `pre-commit` 에 sleep 을 넣어 '살아있는 git' 을 만들려
    했다. 2026-08-30 재현 결과 **잠금이 하나도 잡히지 않았다** — 훅은 잠금을
    잡기 전에 돈다. 느린 원격(push/fetch 대기)도 마찬가지였다.

    이 테스트는 그 사실을 박제한다. 언젠가 git 이 바뀌어 훅 구간에 잠금을
    쥐게 되면 여기가 빨간불이 되고, 그때 '살아있는 git' 재현 수단을 다시
    검토하면 된다. 틀린 가정은 지우는 것보다 **고정해 두는 편**이 낫다.
    """
    import subprocess
    import time

    scn.install_slow_hook(seconds=3.0)
    (scn.work / "held.md").write_text("x", encoding="utf-8")
    sc.git("add", "--", "held.md", cwd=scn.work)

    proc = subprocess.Popen(["git", "commit", "-m", "slow"], cwd=str(scn.work),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        assert not scn.loose_locks(), (
            "훅 구간에서 잠금이 잡혔다 — git 동작이 바뀌었을 수 있다. "
            "'살아있는 git' 재현 수단을 다시 검토할 것")
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_git_holds_locks_only_briefly(scn):
    """실측 고정: 잠금 보유 구간은 밀리초 단위다.

    이것이 §1.2 위협 모델의 근거다 — 잠금이 밀리초만 잡힌다면 **남아 있는
    잠금은 거의 전부 죽은 잔해**다. 실측된 잔해 10건이 전부 0바이트였던 것과
    일치한다. 폴링으로 포착은 되므로 '잠금이 아예 안 생긴다'는 아니다.
    """
    import threading

    seen: list[str] = []
    stop = False

    def watch():
        while not stop:
            seen.extend(p.name for p in scn.loose_locks())

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    for i in range(15):
        scn.commit(f"burst{i}.md", str(i))
    stop = True
    t.join(timeout=5)

    assert seen, "커밋 15회 동안 잠금을 한 번도 포착하지 못했다 — 관측기가 고장"
    # 그러나 커밋이 끝난 뒤에는 하나도 남지 않는다 — 정상 종료는 잔해를 안 남긴다
    assert not scn.loose_locks()


# ---------------------------------------------------------------- 관측
def test_loose_locks_finds_nested_locks(scn):
    scn.make_lock(sc.LockSpec(".git/objects/maintenance.lock", "dead-empty"))
    names = [p.name for p in scn.loose_locks()]
    assert "maintenance.lock" in names
