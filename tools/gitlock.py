"""git 잠금 정책 — **에이전트가 고치는 단 하나의 파일**.

autoresearch 의 `train.py` 에 대응한다. 하버스(`tests/gitlock_harness/`)는
수정 금지이고, 개선은 전부 여기서 일어난다.

## 왜 파일 하나인가

잠금 처리가 두 곳에 복제되어 각자 썩은 것이 2026-08-30 사고의 구조였다:

    agent_base/deploy/lib.ps1:110      $text -match 'index\\.lock'
    knowledge_base/scripts/capture.py:133   if "index.lock" in err:

둘 다 `HEAD.lock` 을 놓쳤고, 그날 실패가 정확히 `HEAD.lock` 이었다. 정책을
여기 한 곳에 모으고 호출부는 얇게 만든다.

**호출부에 정규식을 두지 않는다.** git 이 실패하면 stderr 를 통째로 넘기고
판정은 전부 여기서 한다 — "정규식이 새 잠금 종류를 놓친다"는 결함 자체가
구조적으로 불가능해진다.

## 현재 상태: baseline

이 파일은 지금 **`lib.ps1` 의 동작을 그대로 옮긴 것**이다. 즉 같은 결함을
가지고 있고, 하버스의 R8 이 처음부터 실패한다. 그것이 의도다 — 개선의
출발점이 눈에 보여야 한다.

## 규칙 (program.md 에서 옮김)

- 잠금은 **지우지 말고 옮긴다.** 삭제는 정적 검사에서 100점이다.
- 판정 불가는 진행이 아니라 **멈춤**이다.
- 복구는 성공을 보고하지 않는다.
- 파괴적 명령(`reset --hard`·`merge --abort`·`push --force`)을 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = ["LockState", "Action", "classify", "decide", "quarantine", "handle"]


# ---------------------------------------------------------------- 상태
@dataclass
class LockState:
    """잠금 오류에 대한 판정 결과."""
    is_lock_error: bool = False
    path: Path | None = None          # 문제의 잠금 파일
    name: str = ""                    # 'index.lock' 등
    live: bool | None = None          # True=보유자 있음 / False=잔해 / None=판단 불가
    empty: bool | None = None         # 0바이트인가
    raw: str = ""

    def describe(self) -> str:
        if not self.is_lock_error:
            return "잠금 오류 아님"
        holder = {True: "보유자 있음", False: "죽은 잔해",
                  None: "판단 불가"}[self.live]
        return f"{self.name} — {holder}"


class Action:
    WAIT = "wait"              # 기다렸다 재시도
    QUARANTINE = "quarantine"  # 격리 후 진행
    STOP = "stop"              # 멈추고 사람에게
    PROCEED = "proceed"        # 잠금 문제 아님, 그냥 진행


# ---------------------------------------------------------------- 판정
# 잠금 종류를 **열거하지 않는다.** git 이 잠금 오류를 낼 때 대부분 잠금 파일의
# 경로를 문구에 그대로 싣기 때문이다(2026-08-30 실측):
#
#   index    fatal: Unable to create '<repo>/.git/index.lock': File exists.
#   HEAD     fatal: cannot lock ref 'HEAD': Unable to create '<repo>/.git/HEAD.lock': ...
#   refs     fatal: cannot lock ref 'HEAD': Unable to create '<repo>/.git/refs/heads/main.lock': ...
#   config   error: could not lock config file .git/config: File exists   <- 경로에 .lock 이 없다
#
# 이름 목록을 쓰면 새 잠금 종류가 생길 때마다 목록이 낡는다 — 그것이
# lib.ps1:110 과 capture.py:133 이 함께 앓던 병이다. 경로를 뽑아내면
# 목록을 유지할 필요가 없다.
_LOCK_PATH = re.compile(r"['\"]([^'\"]*\.lock)['\"]")
# config 만 예외 — 경로에 .lock 이 안 붙는다. 이것만 별도로 잡는다.
# `[^:\s]+` 인 이유: 문구가 "...config file .git/config: File exists" 라
# `\S+` 로 받으면 뒤따르는 콜론까지 삼켜 'config:.lock' 이 된다(실측).
_CONFIG_LOCK = re.compile(r"could not lock config file\s+([^:\s]+)", re.IGNORECASE)
# 마지막 그물: 경로를 못 뽑아도 '잠금 실패'라는 것은 알 수 있다.
_LOCK_HINT = re.compile(
    r"cannot lock ref|could not lock|Unable to create.*File exists|"
    r"index\.lock|another git process", re.IGNORECASE)


def _unwrap(text: str) -> str:
    r"""콘솔 너비로 접힌 줄을 되편다.

    Windows PowerShell 5.1 이 네이티브 명령의 stderr 를 수집할 때 **콘솔 너비에서
    줄을 접는다**(실측 2026-08-30). 그래서 경로가 이렇게 쪼개진다:

        ...Temp/diag_917809740/.gi
        t/HEAD.lock': File exists.

    이 상태로는 경로 정규식이 줄바꿈을 넘지 못해 판정이 '판단 불가'로 떨어지고,
    죽은 잔해를 격리하지 못한다. PowerShell 7 에서는 접지 않아 잘 되므로
    **5.1 에서만 조용히 실패하는** 종류의 결함이다 — lib.ps1 이 5.1 호환을
    요구하니(lib.ps1:3) 반드시 다뤄야 한다.

    되펴는 규칙: 줄이 따옴표를 닫지 못한 채 끝나면 다음 줄과 붙인다. 접힘은
    공백 없이 일어나므로 그대로 이어 붙이면 원문이 복원된다.
    """
    if "\n" not in text:
        return text
    out: list[str] = []
    pending = ""
    for line in text.split("\n"):
        merged = pending + line
        # 따옴표가 홀수 개면 아직 경로가 닫히지 않았다 = 접힌 줄이다
        if merged.count("'") % 2 == 1:
            pending = merged
            continue
        out.append(merged)
        pending = ""
    if pending:
        out.append(pending)
    return "\n".join(out)


def classify(stderr: str, repo: Path | str) -> LockState:
    """git stderr 를 보고 잠금 오류인지, 어떤 잠금인지, 산 것인지 판정한다.

    판정 순서가 중요하다. **경로를 먼저 뽑고, 못 뽑으면 힌트로 떨어진다.**
    경로를 얻으면 그 파일을 직접 조사할 수 있고(살아있음·크기), 못 얻으면
    '잠금 문제인 건 알지만 어느 것인지 모른다' = 판단 불가 = 멈춤이다.
    """
    repo = Path(repo)
    state = LockState(raw=stderr or "")
    text = _unwrap(state.raw)

    path: Path | None = None
    m = _LOCK_PATH.search(text)
    if m:
        path = Path(m.group(1))
    else:
        mc = _CONFIG_LOCK.search(text)
        if mc:
            # '.git/config' 를 가리키므로 실제 잠금은 그 옆의 config.lock 이다
            raw = mc.group(1)
            cand = Path(raw) if Path(raw).is_absolute() else repo / raw
            path = cand.with_name(cand.name + ".lock")

    if path is None:
        if not _LOCK_HINT.search(text):
            return state                      # 잠금 오류가 아니다
        # 잠금 문제인 건 아는데 대상을 모른다 — 진행하지 않는다.
        state.is_lock_error = True
        state.name = "(경로 불명)"
        state.live = None
        return state

    if not path.is_absolute():
        path = repo / path
    state.is_lock_error = True
    state.name = path.name
    state.path = path

    if not path.exists():
        # 오류는 났는데 파일이 없다 — 그 사이 누가 치웠다. 판단 불가.
        state.live = None
        return state

    state.empty = path.stat().st_size == 0
    state.live = _is_held(path)
    return state


def _is_held(path: Path) -> bool | None:
    """보유자 유무를 본다. 잠금 획득에 성공하면 아무도 안 쥔 것(=죽은 잔해).

    **파일을 여는 것으로는 판별할 수 없다**(2026-08-30 실측). Windows 의
    바이트 범위 잠금은 mandatory 지만 *열기* 를 막지 않는다 — 잠긴 구간을
    읽거나 다시 잠그려 할 때만 막힌다:

        [다른 프로세스가 바이트 범위 잠금 보유]
          open(path, "r+b")   -> 성공   (판별 불가)
          msvcrt.locking(...) -> PermissionError   <== 진짜 판별기

    첫 구현은 `open()` 성공을 '아무도 안 쥠'으로 읽었고, 그래서 살아있는
    보유자를 죽은 잔해로 판정했다. 그 상태로 채택됐다면 **남의 잠금을 격리하고
    진행**했을 것이다 — 이 프로젝트에서 가장 하면 안 되는 동작이다.
    하버스의 `test_live_holder_is_detected` 가 잡아냈다.

    판정할 수 없으면 None — 그리고 None 은 진행이 아니라 멈춤이다.
    """
    if sys.platform != "win32":
        return None
    import msvcrt

    try:
        fh = open(path, "r+b")
    except PermissionError:
        return True          # 열지도 못한다 = 확실히 누가 쥐고 있다
    except OSError:
        return None
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return True          # 잠글 수 없다 = 보유자 있음
    else:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return False         # 잠글 수 있었다 = 아무도 안 쥠
    finally:
        try:
            fh.close()
        except OSError:
            pass


# ---------------------------------------------------------------- 결정
def decide(state: LockState) -> str:
    if not state.is_lock_error:
        return Action.PROCEED
    if state.live is True:
        return Action.WAIT
    if state.live is None:
        return Action.STOP          # 판정 불가는 멈춤
    if state.empty:
        return Action.QUARANTINE
    # 내용이 있는 잔해 — 쓰다 만 것이라 무엇을 덮는지 모른다. 격리하되 진행 안 함.
    return Action.STOP


# ---------------------------------------------------------------- 격리
def quarantine(path: Path, repo: Path | str) -> Path:
    """잠금을 지우지 않고 옆으로 옮긴다.

    이름 규칙: `<태그>.<원래mtime>.quarantine`
    기존 `_to_delete/locks/` 의 10건은 규칙이 셋으로 갈려 있었고
    (`HEAD.lock.11674` / `20312.lock` / `.-2103.lock`) 숫자가 Windows PID 도
    아니었다(PID 는 4의 배수). 재사용할 규칙이 없어 새로 정했다.
    """
    repo = Path(repo)
    dest_dir = repo / "_to_delete" / "locks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{path.name}.{stamp}.quarantine"
    n = 0
    while dest.exists():
        n += 1
        dest = dest_dir / f"{path.name}.{stamp}.{n}.quarantine"
    path.rename(dest)
    return dest


# ---------------------------------------------------------------- 진입점
def handle(stderr: str, repo: Path | str) -> dict:
    """호출부가 부르는 유일한 함수. 판정 → 결정 → (필요하면) 격리."""
    state = classify(stderr, repo)
    action = decide(state)
    quarantined: list[str] = []

    if action == Action.QUARANTINE and state.path is not None:
        try:
            quarantined.append(str(quarantine(state.path, repo)))
        except OSError as exc:
            return {"action": Action.STOP, "why": f"격리 실패: {exc}",
                    "state": state.describe(), "quarantined": []}

    return {"action": action, "why": state.describe(),
            "state": state.describe(), "quarantined": quarantined}


def main(argv: list[str] | None = None) -> int:
    """CLI — PowerShell 호출부가 쓴다. stdin 또는 파일로 stderr 를 받는다."""
    ap = argparse.ArgumentParser(description="git 잠금 판정")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--stderr-file")
    args = ap.parse_args(argv)

    if args.stderr_file:
        text = Path(args.stderr_file).read_text(encoding="utf-8", errors="replace")
    else:
        text = sys.stdin.read()

    result = handle(text, args.repo)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
