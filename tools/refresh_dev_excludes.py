"""dev 프로파일의 exclude를 git 추적파일 기준으로 다시 만든다.

## 왜 필요한가

C:\\drive\\dev 는 6개 git 저장소를 담은 부모 폴더다. 두 PC 모두 git 으로
소스를 주고받으므로, git 과 Dooray 가 **같은 추적파일을 양방향으로 나르면**
서로 충돌한다(BookOps 사례: 커밋 81개 중 머지 12개). 그래서 Dooray 는 git 이
나르지 않는 것 — gitignore 된 비밀정보(.env·토큰)와 non-git 폴더 — 만 동기화한다.

그 경계를 지키려면 "git 이 추적하는 파일"을 Dooray exclude 에 넣어야 하는데,
추적 목록은 커밋할 때마다 바뀐다. **새 파일을 커밋했으면 이 스크립트를 다시
돌려** exclude 를 갱신한다. 안 그러면 그 새 파일이 git·Dooray 두 채널로
양방향 이동해 충돌한다.

## 사용

    python tools\\refresh_dev_excludes.py               # 기본: C:\\drive\\dev, 프로파일 dev
    python tools\\refresh_dev_excludes.py --dev-root D:\\drive\\dev --profile dev
    python tools\\refresh_dev_excludes.py --check       # 바꾸지 않고 현재와 차이만 본다

dooray_sync 패키지를 import 하므로 dooraydrive 저장소 안에서 실행하거나
DSYNC_HOME 이 PATH 에 잡혀 있어야 한다.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

# 용량 폐기물 + 기본 제외. 비밀정보는 여기 없다 — gitignore 돼 있어도 Dooray 로
# 올려 PC2 에 전달하는 것이 목적이기 때문이다.
JUNK = ["~$*", "*.tmp", "*.part", "*.crdownload", "Thumbs.db", "desktop.ini",
        ".dooraysync/", ".~lock.*#", ".venv/", "venv/", "__pycache__/",
        ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "node_modules/",
        "dist/", "build/", "*.pyc", ".egg-info/"]


def _git(repo: Path, args: list[str]) -> list[str]:
    # core.quotepath=false: 한글 경로가 \\355.. 로 인용되면 패턴이 깨진다.
    r = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(repo)] + args,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def build_patterns(dev_root: Path) -> list[str]:
    repos = sorted(d.name for d in dev_root.iterdir() if (d / ".git").is_dir())
    patterns: set[str] = set()
    for repo in repos:
        rp = dev_root / repo
        tracked = [f"{repo}/{p}" for p in _git(rp, ["ls-files"])]
        # 추적되지 않은 채 존재하는 파일(비밀정보 포함) — 이 폴더는 통째 제외하면 안 된다
        others = set(f"{repo}/{p}" for p in (
            _git(rp, ["ls-files", "--others", "--exclude-standard"])
            + _git(rp, ["ls-files", "--others", "--ignored", "--exclude-standard"])))

        def excludable(d: str, others=others) -> bool:
            return not any(s == d or s.startswith(d + "/") for s in others)

        for t in tracked:
            parts = t.split("/")
            chosen = None
            for i in range(1, len(parts)):           # 파일 자신(마지막)은 제외
                d = "/".join(parts[:i])
                if excludable(d):
                    chosen = d
                    break
            patterns.add(chosen + "/" if chosen else t)   # 통째 제외 가능한 최상위 디렉터리, 없으면 파일 단위
    return sorted(patterns)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev-root", default=r"C:\drive\dev")
    ap.add_argument("--profile", default="dev")
    ap.add_argument("--check", action="store_true", help="바꾸지 않고 차이만 본다")
    a = ap.parse_args(argv)

    # 이 스크립트는 <repo>/tools/ 에 산다. dooray_sync 를 import 하려면 repo 루트가
    # sys.path 에 있어야 한다(스크립트 실행 시 sys.path[0] 은 tools/ 다).
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from dooray_sync import config as cfg
    except ModuleNotFoundError:
        print("[STOP] dooray_sync 를 import 할 수 없습니다. dooraydrive 저장소의 "
              "tools\\ 에서 실행하거나 DSYNC_HOME 을 PATH 에 두세요.")
        return 1

    dev_root = Path(a.dev_root)
    if not dev_root.is_dir():
        print(f"[STOP] dev 루트가 없습니다: {dev_root}")
        return 1

    git_pats = build_patterns(dev_root)
    want = JUNK + git_pats

    p = cfg.load_config(a.profile)
    cur = list(p.exclude or [])
    if cur == want:
        print(f"  [OK] [{a.profile}] exclude 이미 최신 ({len(want)}개). 바꾼 것 없음.")
        return 0

    added = [x for x in want if x not in cur]
    removed = [x for x in cur if x not in want]
    print(f"  [{a.profile}] exclude {len(cur)} → {len(want)}개  (+{len(added)} / -{len(removed)})")
    if a.check:
        for x in added[:10]:
            print(f"    + {x}")
        for x in removed[:10]:
            print(f"    - {x}")
        print("  (--check: 바꾸지 않았습니다)")
        return 0

    p.exclude = want
    cfg.save_config(p)
    print(f"  [OK] 저장 완료. 다음 sync 부터 반영됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
