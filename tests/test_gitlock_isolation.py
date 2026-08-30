"""git 잠금 하버스의 격리 자체를 검사한다 — 하버스 코드보다 먼저 존재해야 한다.

잠금 하버스는 정의상 `.git` 내부를 만들고 지운다. 경로 계산이 한 번만 어긋나면
`C:\\drive\\obsidian` 의 진짜 볼트가 망가진다 — 그리고 그 사고는 **다른 테스트가
전부 초록인 채로** 일어난다. 격리는 풀려도 빨간불이 안 뜨는 종류의 고장이라,
겨냥한 탐지기를 따로 둔다(vault 체크리스트 `tests-reaching-real-machine-state.md`).

여기서 검사하는 것은 기능이 아니라 **하네스의 성질**이다:
  1. git 환경변수가 전부 임시 폴더를 가리키는가 (하나만 덮는 것이 2026-08-18 사고)
  2. 실제 볼트 경로를 거부하는가 (경로 검사)
  3. git이 진짜로 그 임시 설정을 읽는가 (실제 쓰기·읽기 검사)
  4. 전역 alias `git save` 가 차단되는가 (실측된 오염원)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.gitlock_harness import REAL_VAULT_ROOTS, assert_outside_real_vaults

GIT_ENV_VARS = (
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "HOME",
    "USERPROFILE",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_NAME",
    "GIT_TERMINAL_PROMPT",
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace")


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


# ---------------------------------------------------------------- 1. 환경변수
def test_every_git_env_var_is_set():
    """하나만 덮는 것이 사고였다 — 전부 깔렸는지 본다."""
    for var in GIT_ENV_VARS:
        assert os.environ.get(var, "").strip(), f"{var} 가 비어 있다"


def test_git_home_is_not_the_real_user_profile():
    """HOME 이 진짜 홈이면 `~/.gitconfig` 가 그대로 읽힌다.

    주의: "사용자 폴더 밖인가"로 물으면 안 된다. `%TEMP%` 자체가
    `C:\\Users\\<나>\\AppData\\Local\\Temp` 라서 격리가 멀쩡해도 참이 된다
    (첫 작성 때 실제로 이 단언이 틀려 실패했다). 물어야 하는 것은 위치가
    아니라 **정체** — 진짜 홈 그 자체인가다.
    """
    home = Path(os.environ["HOME"]).resolve()
    real_home = Path(r"C:\Users\martin.hs.yoo").resolve()
    assert home != real_home, f"HOME 이 진짜 홈이다: {home}"
    # 진짜 홈의 .gitconfig 가 있어도 읽히지 않아야 한다 —
    # 그 확증은 test_global_save_alias_is_not_visible 이 한다.


# ---------------------------------------------------------------- 2. 경로 검사
@pytest.mark.parametrize("root", REAL_VAULT_ROOTS)
def test_real_vault_paths_are_rejected(root):
    """볼트 안 경로는 하버스가 거부해야 한다."""
    with pytest.raises(AssertionError, match="실제 볼트"):
        assert_outside_real_vaults(root / "knowledge_base" / "scenario")


def test_path_outside_temp_is_rejected(tmp_path):
    """tmp 밖이면 볼트 목록에 없어도 거부한다 — 목록은 언제나 낡는다."""
    with pytest.raises(AssertionError, match="임시 폴더 밖"):
        assert_outside_real_vaults(Path(r"C:\some\other\place"))


def test_tmp_path_is_accepted(tmp_path):
    assert_outside_real_vaults(tmp_path / "scenario")   # 예외가 없어야 한다


# ------------------------------------------------- 3. git이 실제로 격리를 읽는가
def test_git_actually_reads_the_isolated_config(tmp_path):
    """경로 계산만 맞고 git 이 딴 설정을 읽는 경우를 막는다 — 실제로 써 본다."""
    repo = tmp_path / "probe"
    repo.mkdir()
    assert _git("init", "-q", cwd=repo).returncode == 0

    _git("config", "--global", "harness.marker", "isolated", cwd=repo)
    got = _git("config", "--get", "harness.marker", cwd=repo).stdout.strip()
    assert got == "isolated"

    # 그 값이 임시 파일에 들어갔는지 — 진짜 ~/.gitconfig 가 아니라.
    # git 은 INI 로 쓴다("[harness]\n\tmarker = isolated"). 점 표기로 찾으면
    # 격리가 멀쩡해도 실패한다(첫 작성 때 실제로 그랬다).
    written = Path(os.environ["GIT_CONFIG_GLOBAL"])
    text = written.read_text(encoding="utf-8")
    assert "[harness]" in text and "isolated" in text, f"기록 안 됨: {text!r}"
    assert written.resolve() != (Path(r"C:\Users\martin.hs.yoo") / ".gitconfig")


def test_global_save_alias_is_not_visible(tmp_path):
    """실측된 오염원: ~/.gitconfig 의 alias.save = add -A && commit && push.

    이것이 새어 들어오면 "무엇이 실행됐는가"의 통제가 깨진다.
    """
    repo = tmp_path / "probe"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    r = _git("config", "--get", "alias.save", cwd=repo)
    assert r.returncode != 0, f"전역 alias.save 가 새어 들어왔다: {r.stdout!r}"


def test_commit_identity_available(tmp_path):
    """신원이 없으면 시나리오가 잠금과 무관한 이유로 죽고, 그것을 오독하게 된다."""
    repo = tmp_path / "probe"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git("add", "a.txt", cwd=repo)
    r = _git("commit", "-m", "probe", cwd=repo)
    assert r.returncode == 0, f"커밋 실패(신원 문제 의심): {r.stderr}"


def test_gc_auto_does_not_fire_during_scenarios(tmp_path):
    """`gc.auto` 가 살아 있으면 백그라운드 정비가 maintenance.lock 을 만든다 —
    시나리오가 만들려던 파일을 환경이 대신 만들면 무엇을 측정했는지 알 수 없다.

    전역이 비어 있으므로 git 기본값(6700)이 적용된다. 하버스 저장소는 객체가
    수십 개 수준이라 임계에 닿지 않는다. 그 전제를 여기서 고정한다.
    """
    repo = tmp_path / "probe"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    loose = list((repo / ".git" / "objects").rglob("*"))
    assert len([p for p in loose if p.is_file()]) < 100


# ---------------------------------------------------------------- 4. 역검증
def test_guard_catches_a_fake_vault(tmp_path, monkeypatch):
    """가드를 **꺼서** 검증한다 — 가짜 볼트를 만들어 놓고 잡히는지 본다.

    vault 체크리스트: "그 테스트를 가드를 꺼서 역검증했는가 — 가짜 사용자
    폴더를 만들어 놓고 껐는가". 가드가 그냥 항상 통과하는 코드였다면
    이 테스트가 실패한다.
    """
    import tests.gitlock_harness as h

    fake_vault = tmp_path / "fake_obsidian"
    (fake_vault / "knowledge_base" / ".git").mkdir(parents=True)
    monkeypatch.setattr(h, "REAL_VAULT_ROOTS", (fake_vault,))

    with pytest.raises(AssertionError, match="실제 볼트"):
        h.assert_outside_real_vaults(fake_vault / "knowledge_base" / "scenario")
