"""시나리오 생성기 — 수정 금지 영역.

각 시나리오는 임시 폴더에 **진짜 git 저장소**를 처음부터 만든다. 페이크를 쓰지
않는 이유는 재현해야 하는 것이 git 자신의 잠금 의미론이기 때문이다 — 흉내 내면
흉내가 맞는지를 다시 검증해야 한다.

구성:

    scenario/
      origin.git/   bare 원격 (push 거부·분기 재현용)
      work/         작업 저장소 (검증 대상이 실행되는 곳)
      work2/        두 번째 PC 역할 (경합·분기 재현용)

잠금 생성 수단:

  죽은 잔해      `Path.touch()` — 프로세스가 필요 없다. "죽은 잔해"의 정의가
                 곧 '보유자 없음'이므로 파일만 있으면 그 상태다.
  살아있는 보유자 배타 열기(msvcrt). 판별기가 쓸 수단을 하버스도 같은 수단으로
                 쓴다 — **판별 수단 자체가 시험된다.**

**계획 §4.3의 '살아있는 git' 재현 수단은 폐기했다(2026-08-30 실측).**
초안은 `.git/hooks/pre-commit` 에 sleep 을 넣어 git 이 잠금을 쥔 채 멈추게
하려 했다. 실제로 재현해 보니 셋 다 틀렸다:

  - `pre-commit` · `prepare-commit-msg` · `commit-msg` 훅이 도는 동안
    `.git` 에 잠금이 **하나도 없다** — 훅은 잠금을 잡기 전에 돈다.
  - 느린 원격(`receivepack`/`uploadpack` 을 sleep 으로 감싼 push·fetch)도
    대기 중 잠금을 쥐지 않는다.
  - git 이 잠금을 쥐는 구간은 **수 밀리초**다. 60회 커밋을 폴링으로 감시해
    60번 포착했으니 실재하지만(index 38 · HEAD 6 · ref 16), 붙잡아 둘 수 있는
    길이가 아니다.

그래서 "살아있는 보유자"는 git 프로세스로 만들지 않고 배타 열기로 만든다.
**판별기 입장에서 둘은 구별되지 않는다** — 판별 기준이 "누가 쥐었는가"가 아니라
"지금 배타로 열리는가"이기 때문이다. 재현하지 못하는 것을 흉내 내는 대신,
판별이 실제로 보는 신호를 정확히 만든다.

부수적으로 이것이 §1.2의 위협 모델을 다시 확인해 준다 — 잠금이 밀리초 단위로만
잡힌다면, 남아 있는 잠금은 거의 전부 **죽은 잔해**다. 실측된 잔해 10건이 전부
0바이트였던 것과 일치한다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import assert_outside_real_vaults

__all__ = ["Scenario", "build", "git", "GitResult", "LockSpec"]


@dataclass
class GitResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        """stdout+stderr 합본. git 은 정상 진행도 stderr 로 낸다."""
        return (self.out + "\n" + self.err).strip()


def git(*args: str, cwd: Path, check: bool = False) -> GitResult:
    """git 호출. 하버스 전용 — 검증 대상이 아니라 **상황을 만드는** 쪽이다."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    r = GitResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    if check and not r.ok:
        raise RuntimeError(f"하버스 셋업 실패: git {' '.join(args)}\n{r.text}")
    return r


@dataclass
class LockSpec:
    """만들 잠금 하나의 명세.

    `holder` 는 계획 §2.2의 H 축이다:
      dead-empty    0바이트 잔해 (보유자 없음)
      dead-partial  내용이 있는 잔해 (쓰다 만 것)
      live-nongit   배타 열기로 붙잡힘
      (live-git 은 pre-commit 훅으로 만든다 — LockSpec 이 아니라 build 인자)
    """
    rel: str                       # '.git/HEAD.lock' 처럼 저장소 기준 상대경로
    holder: str = "dead-empty"
    content: str = ""
    age_sec: float = 0.0           # 파일 mtime 을 과거로 밀어 A 축(나이)을 만든다


@dataclass
class Scenario:
    root: Path
    origin: Path
    work: Path
    work2: Path
    _live_handles: list = field(default_factory=list)

    # ---------------------------------------------------------- 잠금 만들기
    def make_lock(self, spec: LockSpec) -> Path:
        path = self.work / spec.rel
        path.parent.mkdir(parents=True, exist_ok=True)

        if spec.holder == "dead-empty":
            path.touch()
        elif spec.holder == "dead-partial":
            path.write_text(spec.content or "partial write\n", encoding="utf-8")
        elif spec.holder == "live-nongit":
            path.touch()
            self._hold_exclusive(path)
        else:
            raise ValueError(f"알 수 없는 holder: {spec.holder!r}")

        if spec.age_sec:
            past = os.path.getmtime(path) - spec.age_sec
            os.utime(path, (past, past))
        return path

    def _hold_exclusive(self, path: Path) -> None:
        """배타 열기로 붙잡는다 — 판별기가 '살아있음'으로 읽어야 하는 상태.

        `dooray_sync.util.lock.SingleInstanceLock` 과 같은 수단(msvcrt)을 쓴다.
        여기서 직접 여는 이유는 그 클래스가 자기 형식(pid 기록)을 파일에 쓰기
        때문이다 — 잠금 파일의 내용까지 흉내 내면 시나리오가 오염된다.
        """
        if sys.platform != "win32":
            raise RuntimeError("이 하버스는 Windows 전용이다(msvcrt 배타 잠금)")
        import msvcrt

        fh = open(path, "r+b")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            raise
        self._live_handles.append(fh)

    def release_all(self) -> None:
        """붙잡은 핸들을 전부 놓는다. 테스트 종료 시 반드시 부른다."""
        import msvcrt
        for fh in self._live_handles:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass
        self._live_handles.clear()

    # ---------------------------------------------------------- 상태 만들기
    def commit(self, name: str, text: str = "x", *, repo: Path | None = None) -> str:
        """파일 하나를 만들고 커밋. 반환은 커밋 해시."""
        target = repo or self.work
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(text, encoding="utf-8")
        git("add", "--", name, cwd=target, check=True)
        git("commit", "-m", f"add {name}", cwd=target, check=True)
        return git("rev-parse", "HEAD", cwd=target).out.strip()

    def stage(self, name: str, text: str) -> None:
        """커밋하지 않고 스테이지만 — 08-30 실물 상태(R1)를 만드는 데 쓴다."""
        (self.work / name).write_text(text, encoding="utf-8")
        git("add", "--", name, cwd=self.work, check=True)

    def diverge(self) -> None:
        """로컬과 원격을 서로 앞서게 만든다(분기). work2 가 두 번째 PC 역할."""
        self.commit("from_pc2.md", "remote side", repo=self.work2)
        git("push", cwd=self.work2, check=True)
        self.commit("from_pc1.md", "local side")
        git("fetch", "origin", cwd=self.work, check=True)

    def install_slow_hook(self, seconds: float = 5.0) -> None:
        """느린 훅을 깐다.

        **잠금을 붙잡는 용도가 아니다**(모듈 docstring 참조 — 훅이 도는 동안
        git 은 잠금을 쥐지 않는다는 것을 실측했다). 이것이 쓸모 있는 곳은
        "연산이 오래 걸리는 동안 다른 프로세스가 끼어든다"는 **경합 타이밍**을
        만드는 것이다. 잠금 보유 재현으로 쓰지 말 것.
        """
        hook = self.work / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(f"#!/bin/sh\nsleep {seconds}\n", encoding="utf-8")
        hook.chmod(0o755)

    # ---------------------------------------------------------- 관측
    def commits_on(self, ref: str = "HEAD", *, repo: Path | None = None) -> list[str]:
        target = repo or self.work
        r = git("rev-list", ref, cwd=target)
        return r.out.split() if r.ok else []

    def loose_locks(self) -> list[Path]:
        """작업 저장소에 남아 있는 *.lock 전부."""
        return sorted((self.work / ".git").rglob("*.lock"))


def build(tmp_path: Path, *, with_remote: bool = True,
          with_second_pc: bool = False) -> Scenario:
    """시나리오 저장소 일습을 만든다.

    **모든 경로가 `assert_outside_real_vaults` 를 통과한다.** 검사를 호출부마다
    기억하게 두지 않고 여기 한 곳에서 한다 — 기억에 의존하는 안전장치는
    잊는 순간 사라진다(2026-08-18 사고의 형태).
    """
    root = Path(tmp_path) / "scenario"
    assert_outside_real_vaults(root)
    root.mkdir(parents=True, exist_ok=True)

    origin = root / "origin.git"
    work = root / "work"
    work2 = root / "work2"

    if with_remote:
        git("init", "--bare", "-q", "-b", "main", str(origin), cwd=root, check=True)

    git("init", "-q", "-b", "main", str(work), cwd=root, check=True)
    # 첫 커밋 — 빈 저장소는 unborn 이라 대부분의 시나리오가 성립하지 않는다.
    (work / "seed.md").write_text("seed\n", encoding="utf-8")
    git("add", "--", "seed.md", cwd=work, check=True)
    git("commit", "-m", "seed", cwd=work, check=True)

    if with_remote:
        git("remote", "add", "origin", str(origin), cwd=work, check=True)
        git("push", "-u", "origin", "main", cwd=work, check=True)

    if with_second_pc:
        git("clone", "-q", str(origin), str(work2), cwd=root, check=True)

    return Scenario(root=root, origin=origin, work=work, work2=work2)
