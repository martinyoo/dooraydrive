"""`lib.ps1` 의 잠금 정책 결선을 **실제 PowerShell 로** 시험한다.

여기 있는 것은 파이썬 해석기 탐색이다. 사소해 보이지만 이것이 무너지면
§7 의 잠금 격리가 **통째로 죽는다** — 그리고 조용히 죽는다.

적대적 검증이 잡은 것(2026-08-31, 유일한 major):

    Windows 는 `%LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe` 라는
    **0바이트 스토어 별칭**을 기본 활성으로 깐다. 파이썬 설치관리자의
    "Add python.exe to PATH" 는 **기본이 꺼짐**이다. 그렇게 설치한 PC 에서
    `Get-Command python` 은 그 껍데기로 풀린다.

    옛 코드는 그것을 해석기로 믿고 실행했고, 실패하면 하나뿐인 문구
    `잠금 판정 불가(정책 모듈 없음)` 로 물러났다 — **gitlock.py 는 멀쩡히
    거기 있는데도.** 사람은 있는 파일을 찾아 헤맨다.

**이 개발 PC 에서는 보이지 않는 결함이다.** 진짜 파이썬이 PATH 22번,
껍데기가 23번이라 가려진다. 그래서 이 테스트는 PATH 를 갈아끼워 **껍데기만
있는 조건**을 만든다. 안전장치는 그것이 필요한 조건에서 시험한다 — 이
저장소가 반복해 낸 사고 유형 4번이 정확히 그 반대다.

`deploy/README.md` 가 새 PC 준비물로 Git·Obsidian·PAT 만 적고 파이썬을 적지
않으므로, 껍데기만 있는 PC 는 예외가 아니라 **문서가 기술한 표준 상태**다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import machine

LIB, _WHY = machine.resolve("agent_base", "deploy", "lib.ps1")

needs_lib = pytest.mark.skipif(LIB is None, reason=_WHY)

# lib.ps1:3 이 5.1 호환을 요구한다. 5.1 에서만 조용히 실패하는 종류를 이미
# 한 번 겪었으므로(R11, stderr 줄바꿈) 양쪽에서 돌린다.
SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]


def _ps(shell: str, script: str) -> str:
    r = subprocess.run([shell, "-NoProfile", "-Command", script],
                       capture_output=True, timeout=120)
    out = r.stdout.decode("utf-8", "replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", "replace")
    return out


@needs_lib
@pytest.mark.parametrize("shell", SHELLS)
def test_store_stub_is_rejected_and_py_launcher_is_used(shell, tmp_path):
    r"""0바이트 스토어 별칭만 PATH 에 있을 때 그것을 고르면 안 된다.

    껍데기를 실행하면 스토어 창이 뜨거나 비정상 종료하고, 그러면 잠금 판정이
    통째로 물러난다 — 살아있는 잠금이든 죽은 잔해든 전부 그냥 기다리다 실패.
    """
    fake = tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    fake.mkdir(parents=True)
    (fake / "python.exe").touch()          # 0바이트, 실측 껍데기와 같다

    out = _ps(shell, f"""
        . '{LIB}' 2>$null
        $env:PATH = '{fake};C:\\WINDOWS;C:\\WINDOWS\\System32'
        $exe = Resolve-PythonExe
        Write-Output "PICKED=$exe"
    """)
    picked = next(l.split("=", 1)[1].strip()
                  for l in out.splitlines() if l.startswith("PICKED="))

    assert "WindowsApps" not in picked, (
        f"0바이트 스토어 껍데기를 해석기로 골랐다: {picked!r} — "
        "새 PC 에서 잠금 격리가 통째로 죽는다")
    assert picked, "껍데기를 걸렀으면 py 런처로 넘어가야 한다"


@needs_lib
@pytest.mark.parametrize("shell", SHELLS)
def test_fallback_names_the_real_cause(shell, tmp_path):
    """강등 문구가 원인마다 달라야 한다.

    옛 코드는 다섯 원인(저장소 없음·gitlock.py 없음·python 없음·실행 실패·
    파싱 실패)에 문구 하나를 썼고, 그 문구가 하필 '정책 모듈 없음'이라
    **가장 흔한 원인(python 없음)에서 거짓말을 했다.**
    """
    out = _ps(shell, f"""
        . '{LIB}' 2>$null
        $env:PATH = 'C:\\WINDOWS\\System32'
        $r = Invoke-LockPolicy -Repo '{tmp_path.as_posix()}' -Stderr 'fatal: x'
        Write-Output ("JSON=" + ($r | ConvertTo-Json -Compress))
    """)
    payload = json.loads(next(l.split("=", 1)[1]
                              for l in out.splitlines() if l.startswith("JSON=")))

    assert payload["action"] == "wait", "파괴하지 않는 쪽으로 무너져야 한다"
    assert "정책 모듈 없음" not in payload["state"], (
        f"gitlock.py 는 있는데 없다고 말한다: {payload['state']!r}")


@needs_lib
def test_fallback_never_deletes():
    """강등 경로 어디에도 삭제가 없어야 한다 — 실패는 파괴가 아니어야 한다."""
    src = LIB.read_text(encoding="utf-8")
    start = src.index("function Invoke-LockPolicy")
    body = src[start:start + 3000]
    for banned in ("Remove-Item -LiteralPath $lock", "del ", "Remove-Item *.lock"):
        assert banned not in body, f"강등 경로가 삭제를 한다: {banned!r}"
    assert "action = 'wait'" in body, "강등 기본값이 wait 이 아니다"
