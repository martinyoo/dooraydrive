"""무인 실행의 마커 정합 규칙 — 설계 I-A3(무인은 sync_mode를 올리지 않는다)와
I-A4(한 번에 최대 1개만 해제)를 고정한다.

사람 경로의 기본 동작이 바뀌지 않는 것도 함께 고정한다 — 파라미터를 추가하면서
기존 호출측의 의미가 조용히 달라지는 것이 이 저장소의 실증된 사고 유형이다.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dooray_sync import config as cfg  # noqa: E402


def _sync_here():
    spec = importlib.util.spec_from_file_location(
        "sync_here_markers_test", REPO / "tools" / "sync_here.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cfgdir(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_CONFIG_DIR, str(tmp_path / "cfg"))
    monkeypatch.setenv(cfg.ENV_STATE_DIR, str(tmp_path / "state"))
    return tmp_path


def _mk(name: str, root: Path, mode: str, note: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    cfg.save_config(cfg.Profile(name=name, drive_id="d", local_root=str(root),
                                remote_path=name, sync_mode=mode, sync_note=note))


def _info(name: str, root: Path, mode: str, note: str = "") -> dict:
    return {"root": str(root), "mode": mode, "note": note, "remote": name,
            "drive_id": "d", "base_url": ""}


def test_override_none_blocks_disable(cfgdir):
    """히스테리시스 미달을 None(판단 불가)으로 주입하면 해제되지 않는다."""
    sh = _sync_here()
    root = cfgdir / "a"
    _mk("a", root, "sync")                       # 마커 파일은 만들지 않는다(부재)
    profiles = {"a": _info("a", root, "sync")}

    changed, failed = sh.reconcile_markers(profiles, state_override={"a": None})

    assert changed == [] and failed == []
    assert cfg.load_config("a").sync_mode == "sync"      # config 그대로
    assert profiles["a"]["mode"] == "sync"


def test_override_false_disables(cfgdir):
    """연속 부재가 확정되면(False 주입) 평소대로 해제된다."""
    sh = _sync_here()
    root = cfgdir / "a"
    _mk("a", root, "sync")
    profiles = {"a": _info("a", root, "sync")}

    changed, _failed = sh.reconcile_markers(profiles, state_override={"a": False})

    assert changed == [("a", "off")]
    assert cfg.load_config("a").sync_mode == "off"


def test_allow_reenable_false_keeps_off(cfgdir):
    """I-A3: 마커가 돌아와도 무인 실행은 sync로 올리지 않는다.

    재등록 분기는 직전 모드를 복원하지 않고 무조건 sync로 보내므로, 사람이
    push/off로 내린 결정을 무인이 뒤집는 경로가 된다.
    """
    sh = _sync_here()
    root = cfgdir / "a"
    (root).mkdir(parents=True, exist_ok=True)
    (root / sh.MARKER).write_text("x", encoding="ascii")   # 마커 있음
    note = f"{sh.AUTO_OFF_PREFIX} 2026-08-16 로컬 없음"
    _mk("a", root, "off", note)
    profiles = {"a": _info("a", root, "off", note)}

    changed, _failed = sh.reconcile_markers(profiles, allow_reenable=False)

    assert changed == []
    assert cfg.load_config("a").sync_mode == "off"

    # 사람 경로(기본값)에서는 예전처럼 재등록된다 — 기본 동작 불변 확인
    changed2, _f2 = sh.reconcile_markers(profiles)
    assert changed2 == [("a", "sync")]
    assert cfg.load_config("a").sync_mode == "sync"


def test_max_disable_blocks_mass_deregistration(cfgdir):
    """I-A4: 2개가 동시에 사라지면 공통 마운트 장애로 보고 아무것도 안 지운다."""
    sh = _sync_here()
    roots = {}
    profiles = {}
    for name in ("a", "b"):
        root = cfgdir / name
        _mk(name, root, "sync")
        roots[name] = root
        profiles[name] = _info(name, root, "sync")

    changed, failed = sh.reconcile_markers(
        profiles, state_override={"a": False, "b": False}, max_disable=1)

    assert changed == [] and failed == []
    assert cfg.load_config("a").sync_mode == "sync"
    assert cfg.load_config("b").sync_mode == "sync"


def test_max_disable_allows_single(cfgdir):
    sh = _sync_here()
    for name in ("a", "b"):
        _mk(name, cfgdir / name, "sync")
    profiles = {
        "a": _info("a", cfgdir / "a", "sync"),
        "b": _info("b", cfgdir / "b", "sync"),
    }
    changed, _failed = sh.reconcile_markers(
        profiles, state_override={"a": False, "b": True}, max_disable=1)

    assert changed == [("a", "off")]
    assert cfg.load_config("b").sync_mode == "sync"


def test_auto_profiles_requires_both_flags(cfgdir):
    """auto_sync와 sync_mode는 AND — 한쪽만으로는 자동 대상이 아니다."""
    from dooray_sync.auto.runner import auto_profiles

    cfg.save_config(cfg.Profile(name="both", drive_id="d",
                                local_root=str(cfgdir / "both"),
                                auto_sync=True, sync_mode="sync"))
    cfg.save_config(cfg.Profile(name="offmode", drive_id="d",
                                local_root=str(cfgdir / "offmode"),
                                auto_sync=True, sync_mode="off"))
    cfg.save_config(cfg.Profile(name="manual", drive_id="d",
                                local_root=str(cfgdir / "manual"),
                                auto_sync=False, sync_mode="sync"))

    assert auto_profiles() == ["both"]
