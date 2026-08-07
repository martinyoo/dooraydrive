"""tools/sync_here.py 마커↔config 정합 회귀 테스트 (2026-08-07 사용자 요구).

로컬 마커(프로파일 루트의 synchere.bat)는 등록/해제 스위치다:
  - sync 프로파일의 마커가 지워지면 자동 해제(off + 태그, soft-delete)
  - 태그 off 프로파일의 마커가 되살아나면 자동 재등록(sync 복귀)
  - push/pull/태그 없는 off(사람의 결정)는 절대 자동 전환하지 않는다
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dooray_sync import config as cfg   # noqa: E402

_SH = None


def _sync_here():
    """tools/는 패키지가 아니라 파일 경로로 1회 로드해 캐시한다."""
    global _SH
    if _SH is None:
        spec = importlib.util.spec_from_file_location(
            "sync_here_under_test", REPO / "tools" / "sync_here.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SH = mod
    return _SH


@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(tmp_path / "cfgdir"))
    return tmp_path


def _mk_profile(name: str, root: Path, mode: str, note: str = "") -> None:
    cfg.save_config(cfg.Profile(name=name, drive_id="d", local_root=str(root),
                                remote_path=f"WORK/{name}",
                                sync_mode=mode, sync_note=note))


def _root(tmp_path: Path, name: str, *, marker: bool) -> Path:
    root = tmp_path / name
    root.mkdir()
    if marker:
        (root / "synchere.bat").write_bytes(b"@echo off")
    return root


def test_sync_without_marker_is_auto_deregistered(cfgdir):
    """마커 삭제 = 동기화 대상 해제. 단 soft-delete다 — 프로파일과 기준선은 남고
    sync_mode=off + 태그 노트로 기록된다(일시 부재·재등록 대비)."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=False)
    _mk_profile("a", root, "sync")

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles)

    assert ("a", "off") in changed and not failed
    assert profiles["a"]["mode"] == "off"          # 이번 실행에서도 즉시 제외
    saved = cfg.load_config("a")
    assert saved.sync_mode == "off"
    assert saved.sync_note.startswith(sh.AUTO_OFF_PREFIX)
    assert cfg.config_exists("a")                  # 항목 삭제가 아니라 off


def test_sync_with_marker_is_untouched(cfgdir):
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=True)
    _mk_profile("a", root, "sync")

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles)

    assert changed == [] and failed == []
    assert cfg.load_config("a").sync_mode == "sync"


def test_dry_run_reports_but_never_writes_config(cfgdir):
    """dry-run은 config를 쓰지 않는다. 해제는 미리보기 일관성을 위해 메모리에서만
    제외하고, 재등록은 메모리에서도 하지 않는다(기록 없인 CLI 게이트가 거부하므로)."""
    sh = _sync_here()
    gone = _root(cfgdir, "gone", marker=False)
    back = _root(cfgdir, "back", marker=True)
    _mk_profile("g", gone, "sync")
    _mk_profile("b", back, "off", note=f"{sh.AUTO_OFF_PREFIX} 자동 해제")

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles, dry_run=True)

    assert ("g", "off") in changed and failed == []
    assert profiles["g"]["mode"] == "off"          # 메모리 제외만
    assert profiles["b"]["mode"] == "off"          # 재등록은 예고만
    assert cfg.load_config("g").sync_mode == "sync"    # 기록은 그대로
    assert cfg.load_config("b").sync_mode == "off"


def test_tagged_off_with_marker_is_reenabled(cfgdir):
    """자동 해제된 프로파일에 마커를 다시 복사하고 실행하면 sync로 복귀한다."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=True)
    _mk_profile("a", root, "off", note=f"{sh.AUTO_OFF_PREFIX} 2026-08-07 자동 해제")

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles)

    assert ("a", "sync") in changed and failed == []
    assert profiles["a"]["mode"] == "sync"
    assert cfg.load_config("a").sync_mode == "sync"


def test_manual_modes_are_never_auto_flipped(cfgdir):
    """push/pull/태그 없는 off는 사람의 결정 — 마커가 있든 없든 손대지 않는다.
    (workenv의 1GB 수신 회피·writing의 안전 보류가 더블클릭 한 번에 뒤집히면 안 됨)"""
    sh = _sync_here()
    cases = [
        ("push_nomark", "push", False),
        ("push_marked", "push", True),
        ("pull_nomark", "pull", False),
        ("off_nomark", "off", False),
        ("off_marked", "off", True),   # 태그 없는 off + 마커: 수동 결정이 이긴다
    ]
    for name, mode, marker in cases:
        _mk_profile(name, _root(cfgdir, name, marker=marker), mode, note="사람의 결정")

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles)

    assert changed == [] and failed == []
    for name, mode, _marker in cases:
        assert cfg.load_config(name).sync_mode == mode


def test_tagged_off_without_marker_stays_off(cfgdir):
    """자동 해제 상태에서 마커가 여전히 없으면 아무것도 바꾸지 않는다(멱등)."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=False)
    _mk_profile("a", root, "off", note=f"{sh.AUTO_OFF_PREFIX} 자동 해제")

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles)

    assert changed == [] and failed == []
    assert cfg.load_config("a").sync_mode == "off"


def test_missing_root_folder_counts_as_no_marker(cfgdir):
    """루트 폴더 자체가 사라진 경우도 마커 없음 = 해제. 존재하지 않는 폴더를
    계속 동기화 대상으로 두는 것이 더 위험하다(오프라인 드라이브 등도 soft라 복구 가능)."""
    sh = _sync_here()
    _mk_profile("a", cfgdir / "never_made", "sync")

    profiles = sh._load_profiles()
    changed, _failed = sh.reconcile_markers(profiles)

    assert ("a", "off") in changed
    assert cfg.load_config("a").sync_mode == "off"


def test_unknown_marker_state_changes_nothing(cfgdir, monkeypatch):
    """권한·잠금으로 마커 상태를 판정할 수 없으면 config를 바꾸지 않는다 —
    os.path.exists가 오류를 False로 뭉개 '백신 잠금 순간'을 삭제로 오독하던
    함정(config.py _read_doc과 같은 교훈)의 회귀 방지."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=True)
    _mk_profile("a", root, "sync")

    def _denied(_p):
        raise PermissionError(13, "잠김")
    monkeypatch.setattr(sh, "ext_path", _denied)

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles)

    assert changed == [] and failed == []
    assert profiles["a"]["mode"] == "sync"          # 메모리도 유지 — 이번 실행 계속
    assert cfg.load_config("a").sync_mode == "sync"


def test_persist_failure_is_reported_in_failed_and_stays_excluded(cfgdir, monkeypatch):
    """config 기록 실패는 failed로 보고돼야 한다(SYNC.ps1이 의존하는 유일한
    프로세스 경계 fail-closed 장치). 메모리 제외는 유지된다."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=False)
    _mk_profile("a", root, "sync")
    monkeypatch.setattr(cfg, "save_config",
                        lambda p: (_ for _ in ()).throw(RuntimeError("잠김")))

    profiles = sh._load_profiles()
    changed, failed = sh.reconcile_markers(profiles)

    assert failed == ["a"] and changed == []
    assert profiles["a"]["mode"] == "off"           # fail-closed: 이번 실행 제외
    # 기록은 안 됐다 — undo()는 픽스처의 ENV_CONFIG_DIR까지 되돌리므로 파일을 직접 읽는다
    import tomllib
    with open(cfg.config_path(), "rb") as f:
        assert tomllib.load(f)["profile"]["a"]["sync_mode"] == "sync"


# ---------------------------------------------------------------------------
# main() 결선 — reconcile 호출이 빠지는 mutation을 잡는 통합 테스트
# (적대 검증 지적: 단위 테스트만으로는 320행 호출을 지워도 전부 통과했다)
# ---------------------------------------------------------------------------

def test_main_deregisters_and_does_not_run_sync(cfgdir, monkeypatch):
    """마커 지운 프로파일 루트에서 실행: config에 off 기록, sync는 돌지 않고 rc 2."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=False)
    _mk_profile("a", root, "sync")
    calls: list[str] = []
    monkeypatch.setattr(sh, "_run_sync", lambda n, r, e: calls.append(n) or 0)

    rc = sh.main(["--root", str(root)])

    assert rc == 2 and calls == []
    saved = cfg.load_config("a")
    assert saved.sync_mode == "off"
    assert saved.sync_note.startswith(sh.AUTO_OFF_PREFIX)


def test_main_reenables_and_runs_sync(cfgdir, monkeypatch):
    """태그 off + 마커 재복사 후 실행: config sync 복귀 + 실제로 동기화가 돈다."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=True)
    _mk_profile("a", root, "off", note=f"{sh.AUTO_OFF_PREFIX} 자동 해제")
    calls: list[str] = []
    monkeypatch.setattr(sh, "_run_sync", lambda n, r, e: calls.append(n) or 0)

    rc = sh.main(["--root", str(root)])

    assert rc == 0 and calls == ["a"]
    assert cfg.load_config("a").sync_mode == "sync"


def test_main_dry_run_reenable_exits_zero_without_writing(cfgdir, monkeypatch):
    """재등록 예정뿐인 dry-run은 rc 0(실제 실행이 성공할 것이므로) + config 무기록."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=True)
    _mk_profile("a", root, "off", note=f"{sh.AUTO_OFF_PREFIX} 자동 해제")
    calls: list[str] = []
    monkeypatch.setattr(sh, "_run_sync", lambda n, r, e: calls.append(n) or 0)

    rc = sh.main(["--root", str(root), "--dry-run"])

    assert rc == 0 and calls == []
    assert cfg.load_config("a").sync_mode == "off"


def test_check_markers_exit_codes_and_emit_modes(cfgdir, monkeypatch, tmp_path):
    """--check-markers: 정상 0 / 기록 실패 1 (SYNC.ps1 중단 계약).
    --emit-modes는 유효 mode와 재등록 예정 플래그를 탭 구분으로 쓴다."""
    sh = _sync_here()
    gone = _root(cfgdir, "gone", marker=False)
    back = _root(cfgdir, "back", marker=True)
    _mk_profile("g", gone, "sync")
    _mk_profile("b", back, "off", note=f"{sh.AUTO_OFF_PREFIX} 자동 해제")

    emit = tmp_path / "modes.tsv"
    rc = sh.main(["--check-markers", "--dry-run", "--emit-modes", str(emit)])
    assert rc == 0
    rows = {ln.split("\t")[0]: ln.split("\t")[1:] for ln in
            emit.read_text(encoding="utf-8").splitlines()}
    assert rows["g"] == ["off", "0"]                # 해제 예정 → 유효 mode off
    assert rows["b"] == ["off", "1"]                # 재등록 예정 플래그
    assert cfg.load_config("g").sync_mode == "sync"  # dry-run: 기록 없음

    monkeypatch.setattr(cfg, "save_config",
                        lambda p: (_ for _ in ()).throw(RuntimeError("잠김")))
    assert sh.main(["--check-markers"]) == 1


def test_ensure_local_marker_places_and_is_idempotent(cfgdir):
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=False)
    sh._ensure_local_marker(str(root))
    assert (root / "synchere.bat").exists()
    sh._ensure_local_marker(str(root))              # 두 번째는 무해
    assert (root / "synchere.bat").read_bytes() == \
        (REPO / "synchere.bat").read_bytes()


def test_main_dry_run_reports_dereg_once_without_past_tense(cfgdir, monkeypatch, capsys):
    """dry-run 출력 자기모순 방지: '[해제 예정](기록 안 함)' 직후 과거형
    '[제외] ... 자동 해제됨'이 다시 찍히면 안 된다 — main()의 _reconcile_reported
    가드를 지우는 mutation이 2차 검증에서 침묵 생존했던 것의 회귀 테스트."""
    sh = _sync_here()
    root = _root(cfgdir, "folder", marker=False)
    _mk_profile("a", root, "sync")
    monkeypatch.setattr(sh, "_run_sync", lambda n, r, e: 0)

    sh.main(["--root", str(root), "--dry-run"])

    out = capsys.readouterr().out
    assert "[해제 예정]" in out
    assert "자동 해제됨" not in out        # _explain_skip의 과거형 중복 보고 금지


def test_main_returns_1_when_marker_persist_fails(cfgdir, monkeypatch):
    """기록 실패는 진입점과 무관하게 rc=1이어야 한다(배치 ANY-KEY 재시도 루프의
    조건). runnable이 비는 단일 대상 경로에서 rc=2로 새던 결함(2차 검증)과,
    main 끝의 rc=1 집계를 지우는 mutation이 생존하던 공백의 회귀 테스트."""
    sh = _sync_here()
    parent = cfgdir / "parent"
    parent.mkdir()
    gone = parent / "gone"
    gone.mkdir()
    ok = parent / "ok"
    ok.mkdir()
    (ok / "synchere.bat").write_bytes(b"@echo off")
    _mk_profile("gone", gone, "sync")
    _mk_profile("ok", ok, "sync")
    monkeypatch.setattr(sh, "_run_sync", lambda n, r, e: 0)
    monkeypatch.setattr(cfg, "save_config",
                        lambda p: (_ for _ in ()).throw(RuntimeError("잠김")))

    # 단일 대상(runnable 공집합) — 예전엔 2로 새서 재시도 루프가 무력화됐다
    assert sh.main(["--root", str(gone)]) == 1
    # 다른 프로파일이 성공해도 기록 실패는 1로 남는다
    assert sh.main(["--root", str(parent)]) == 1


def test_sibling_registration_places_marker(cfgdir, monkeypatch):
    """형제 유도 자동 등록 직후 루트에 마커가 놓여야 한다('등록 = 마커 ON' 불변식).
    호출 지점(main 결선)을 지우는 mutation이 생존하던 공백의 회귀 테스트."""
    import types
    sh = _sync_here()
    parent = cfgdir / "parent"
    parent.mkdir()
    sib = parent / "sib"
    sib.mkdir()
    _mk_profile("sib", sib, "sync")          # remote_path='WORK/sib' → 유도 근거
    new = parent / "newfolder"
    new.mkdir()
    monkeypatch.setattr(sh, "subprocess",
                        types.SimpleNamespace(call=lambda *a, **k: 0))
    monkeypatch.setattr(sh, "_run_sync", lambda n, r, e: 0)

    rc = sh.main(["--root", str(new)])

    assert rc == 0
    assert (new / "synchere.bat").exists()   # 없으면 다음 정합이 도로 해제한다


# ---------------------------------------------------------------------------
# set_sync_mode 연동 — 안내되는 공식 전환 경로가 마커 규칙과 어긋나지 않아야 한다
# ---------------------------------------------------------------------------

_SSM = None


def _set_sync_mode_mod():
    global _SSM
    if _SSM is None:
        spec = importlib.util.spec_from_file_location(
            "set_sync_mode_under_test", REPO / "tools" / "set_sync_mode.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SSM = mod
    return _SSM


def test_set_sync_mode_sync_places_marker(cfgdir, monkeypatch):
    """set_sync_mode X sync를 따르면 마커가 놓여야 한다 — 없으면 다음 정합이
    방금 켠 sync를 도로 꺼서 안내 경로가 왕복 루프가 된다(적대 검증 지적)."""
    ssm = _set_sync_mode_mod()
    root = _root(cfgdir, "folder", marker=False)
    _mk_profile("a", root, "off", note="사람의 결정")
    monkeypatch.setattr(sys, "argv", ["set_sync_mode.py", "a", "sync"])

    assert ssm.main() == 0
    assert cfg.load_config("a").sync_mode == "sync"
    assert (root / "synchere.bat").exists()          # 등록 = 마커 ON 불변식


def test_set_sync_mode_explicit_set_clears_auto_tag(cfgdir, monkeypatch):
    """자동 해제 후 사람이 off를 명시하면 태그가 걷혀야 한다 — 태그가 남으면
    GDrive 복원 등으로 마커가 되살아난 순간 자동 재등록이 결정을 뒤집는다."""
    sh = _sync_here()
    ssm = _set_sync_mode_mod()
    root = _root(cfgdir, "folder", marker=False)
    _mk_profile("a", root, "off", note=f"{sh.AUTO_OFF_PREFIX} 자동 해제")
    monkeypatch.setattr(sys, "argv", ["set_sync_mode.py", "a", "off"])

    assert ssm.main() == 0
    saved = cfg.load_config("a")
    assert saved.sync_mode == "off"
    assert not saved.sync_note.startswith(sh.AUTO_OFF_PREFIX)

    # 이제 마커가 되살아나도 자동 재등록되지 않는다
    (root / "synchere.bat").write_bytes(b"@echo off")
    profiles = sh._load_profiles()
    changed, _failed = sh.reconcile_markers(profiles)
    assert changed == []
    assert cfg.load_config("a").sync_mode == "off"
