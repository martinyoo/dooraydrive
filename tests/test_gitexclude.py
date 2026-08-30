"""§7-3 — `.git/` 을 ALWAYS_EXCLUDE 에 넣는다.

**잠복 위험을 닫는 한 줄.** 지금은 vault 가 dsync 프로파일에 없어 발동하지
않지만, `knowledge_base\\synchere.bat` 이 그 자체로 등록 스위치이고
`git+dooray-synchere.bat:38` 이 그것을 호출한다. 08-18에 신중히 넣었던
`.git/` exclude 는 현재 config 에서 사라졌다(프로파일이 옛 6개로 되돌아갔다).

그 배치를 지금 실행하면 `.git` 내부가 동기화 대상이 되고, **원격에 올라간
잠금이 다음 pull 때 되살아난다.** 잠금 잔해가 PC 사이를 옮겨 다니는 최악의
경로다. 설정에 기대지 않고 코드에서 막는다 — 설정은 PC 마다 다르고 사라진다.
"""
from __future__ import annotations

from pathlib import Path

from dooray_sync.core.scanner import ALWAYS_EXCLUDE, LocalScanner
from dooray_sync.util.paths import matches_any


def test_git_dir_is_in_always_exclude():
    assert ".git/" in ALWAYS_EXCLUDE


def test_scanner_skips_git_directory(tmp_path):
    """로컬 스캐너가 `.git` 하위를 한 건도 내놓지 않는다."""
    root = tmp_path / "repo"
    (root / ".git" / "objects" / "ab").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "index.lock").write_text("", encoding="utf-8")
    (root / ".git" / "objects" / "ab" / "cdef").write_bytes(b"\x00obj")
    (root / "note.md").write_text("real content", encoding="utf-8")

    entries = LocalScanner(root, []).scan()
    names = {e.rel_path for e in entries.values()}

    assert "note.md" in names
    assert not [n for n in names if n.startswith(".git")], f"누출: {names}"


def test_nested_repo_git_is_also_excluded(tmp_path):
    """중첩 저장소(bookops)의 `.git` 도 걸린다 — 컴포넌트 매칭."""
    root = tmp_path / "repo"
    (root / "bookops" / ".git" / "refs").mkdir(parents=True)
    (root / "bookops" / ".git" / "HEAD").write_text("x", encoding="utf-8")
    (root / "bookops" / "chapter.md").write_text("keep", encoding="utf-8")

    entries = LocalScanner(root, []).scan()
    names = {e.rel_path for e in entries.values()}

    assert "bookops/chapter.md" in names
    assert not [n for n in names if ".git" in n.split("/")], f"누출: {names}"


def test_gitignore_and_gitattributes_are_not_excluded(tmp_path):
    """오탐 방지 — `.gitignore` 는 정상 파일이고 동기화되어야 한다."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text("x", encoding="utf-8")
    (root / ".gitattributes").write_text("y", encoding="utf-8")

    names = {e.rel_path for e in LocalScanner(root, []).scan().values()}

    assert ".gitignore" in names and ".gitattributes" in names


def test_git_as_plain_file_is_excluded():
    """worktree/submodule 은 `.git` 이 디렉터리가 아니라 **파일**이다.

    패턴이 디렉터리만 잡으면 그 파일이 동기화되고, 그 안의 절대경로가
    다른 PC 로 건너가 저장소를 깨뜨린다.
    """
    assert matches_any(".git", ALWAYS_EXCLUDE)


def test_remote_axis_uses_the_same_list():
    """원격 축도 같은 목록을 쓴다 — 로컬만 막으면 원격 사본이 '신규'로 보인다."""
    import inspect

    from dooray_sync.core import remote

    src = inspect.getsource(remote)
    assert "ALWAYS_EXCLUDE" in src


def test_git_subpaths_match():
    """경로 형태를 바꿔 가며 확인 — 실제 스캔은 rel_path 로 판정한다."""
    for rel in (".git", ".git/HEAD", ".git/objects/ab/cd",
                "bookops/.git", "bookops/.git/index.lock"):
        assert matches_any(rel, ALWAYS_EXCLUDE), rel
