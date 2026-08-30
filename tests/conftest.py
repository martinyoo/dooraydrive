"""테스트가 이 PC의 진짜 설정·상태에 닿지 못하게 막는다.

`_config_dir()`/`_state_root()`는 환경변수가 없으면 %APPDATA%·%LOCALAPPDATA%로
떨어진다(config.py). 그래서 **격리는 각 테스트가 기억해야 하는 예의**였고,
기억하지 못한 테스트는 조용히 실제 운영 데이터를 건드렸다.

실측 2026-08-18 (개발 PC HSY): `test_synchere.py`의 픽스처가 ENV_CONFIG_DIR만
덮고 ENV_STATE_DIR을 빠뜨려서, `sh.main()`을 부르는 테스트가 실행될 때마다
`%LOCALAPPDATA%\\dooray-sync\\auto\\notices.jsonl`을 **실제로 다시 썼다**
(`main()` → `_clear_auto_notices()` → `notify.clear()`는 지울 게 없어도 파일을
새로 쓴다). 자동 루프가 도는 PC에서 pytest를 한 번 돌리면 사람이 아직 보지
못한 '삭제 대기·충돌 대기' 통지가 사라진다. 하필 이 PC의 실제 프로파일 이름이
`folder`이고, 한글 폴더의 자동 생성 이름도 `folder`라 정확히 겹친다.

읽기 사고(AutoState({})가 진짜 state.json을 읽던 c760aa6)와 같은 계열이되
이쪽은 **쓰기**다. 그래서 그 자리만 고치지 않고 여기서 전역으로 막는다 —
앞으로 추가되는 테스트는 격리를 기억할 필요가 없고, 잊어도 새지 않는다.

개별 테스트가 자기 tmp_path로 다시 덮어쓰는 것은 그대로 동작한다(이 픽스처는
바닥값만 깔아 둔다).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dooray_sync import config as cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_user_dirs(tmp_path_factory, monkeypatch):
    """모든 테스트의 설정·상태 루트를 임시 폴더로 내린다(autouse).

    테스트마다 새 폴더를 준다 — 공유하면 한 테스트가 남긴 state.json이
    다음 테스트의 판정을 바꾸는, 지금 막으려는 바로 그 사고가 테스트끼리
    다시 생긴다.
    """
    base = tmp_path_factory.mktemp("userdirs")
    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(base / "cfg"))
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(base / "state"))


@pytest.fixture(autouse=True)
def _isolate_git_env(tmp_path_factory, monkeypatch):
    """git 환경을 테스트마다 새 임시 폴더로 내린다(autouse).

    잠금 하버스가 진짜 git 바이너리를 부르면서 생긴 **새 위험 표면**이다.
    막지 않으면 이 PC의 실제 설정이 시나리오 판정을 바꾼다:

    - `~/.gitconfig` 의 전역 alias `save` = `add -A && commit && push`
      (실측 2026-08-30). 하버스가 만든 저장소에서 이것이 살아 있으면
      "무엇이 실행됐는가"의 통제가 깨진다.
    - `core.autocrlf` · `core.longpaths` · `gc.auto` · hooks 경로 · 서명 설정이
      전부 전역에서 새어 들어온다. 특히 `gc.auto` 는 백그라운드 정비를 깨워
      `maintenance.lock` 을 만든다 — 시나리오가 만들려던 바로 그 파일을
      환경이 대신 만들어 버리면 무엇을 측정했는지 알 수 없다.
    - 커밋에는 신원이 필요하다. 없으면 시나리오가 git 오류로 죽는데, 그것을
      '잠금 처리 실패'로 오독하게 된다.

    `GIT_CONFIG_NOSYSTEM` 까지 세우는 이유는 Program Files 의 시스템 설정도
    같은 경로로 새기 때문이다. 하나만 덮는 것이 2026-08-18 사고였다.
    """
    base = tmp_path_factory.mktemp("gitenv")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    (base / "global.gitconfig").write_text("", encoding="utf-8")

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(base / "global.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg"))
    # 커밋 신원 — 없으면 시나리오가 잠금과 무관한 이유로 죽는다.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "harness")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "harness@test.local")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "harness")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "harness@test.local")
    # 사람을 기다리는 프롬프트를 원천 차단한다(자격 증명·에디터).
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_EDITOR", "true")
