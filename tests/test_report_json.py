"""--report-json — 화면과 보고가 한 벌의 계산에서 나오는지(설계 I-A10) 고정.

페이크 클라이언트 e2e는 test_m2.py의 기반을 재사용하지 않고, 여기서는 순수 함수
(_plan_report/_write_report_json)와 옵션 배선만 고정한다 — 실계정 관찰은 M3
단위 3의 수동 검증 항목이다(계획서 검증 규율).
"""
from __future__ import annotations

import json

from dooray_sync.cli.main import _plan_report, _write_report_json
from dooray_sync.core.differ import DiffStats
from dooray_sync.core.planner import Action, Plan


class _FakeView:
    entries = {"a": 1, "b": 2}
    deleted_keys = ["x"]
    moved_out_keys = []
    changes_seen = 7
    subtrees_relisted = 1
    truncated = False


def _mk_plan() -> Plan:
    pl = Plan()
    pl.actions = [Action(kind="upload_new", rel_path="a.txt", key="a.txt")]
    pl.counts = {"upload_new": 1}
    pl.bytes_up = 10
    pl.delete_count = 3
    pl.delete_actions = 1
    pl.reports = [("r", "이유")]
    pl.protected = []
    pl.unsyncable = []
    pl.deferred = []
    return pl


def test_plan_report_mirrors_plan_objects():
    pl = _mk_plan()
    stats = DiffStats(unchanged=5)
    stats.md5_probes = 2
    stats.md5_probe_skipped = 1
    rep = _plan_report(pl, stats, _FakeView(), cursor_before=10, cursor_after=12,
                       use_full=False, why_full="")
    # 같은 객체에서 나온 값 — 화면(_print_sync_plan)이 찍는 숫자와 동일할 수밖에 없다
    assert rep["plan"]["actions"] == len(pl.actions)
    assert rep["plan"]["delete_count"] == pl.delete_count
    assert rep["plan"]["counts"] == pl.counts
    assert rep["stats"]["unchanged"] == stats.unchanged
    assert rep["stats"]["md5_probe_skipped"] == stats.md5_probe_skipped
    assert rep["remote"]["observed"] == len(_FakeView.entries)
    assert rep["remote"]["changes_seen"] == 7
    assert rep["cursor"] == {"before": 10, "after": 12, "advanced": True}
    assert rep["mode"] == "delta"


def test_cursor_not_advanced_is_visible():
    """커서 미전진(R11 계열 신호)이 JSON에 드러나야 러너가 연속 관측으로 판정한다."""
    rep = _plan_report(_mk_plan(), DiffStats(), _FakeView(), cursor_before=10,
                       cursor_after=10, use_full=False, why_full="")
    assert rep["cursor"]["advanced"] is False


def test_write_report_json_atomic_utf8(tmp_path):
    dest = tmp_path / "sub" / "report.json"
    _write_report_json(str(dest), {"profile": "한글", "n": 1})
    got = json.loads(dest.read_text(encoding="utf-8"))
    assert got == {"profile": "한글", "n": 1}
    # 임시파일이 남지 않는다(원자 교체)
    assert [p.name for p in dest.parent.iterdir()] == ["report.json"]


def test_write_report_json_empty_path_is_noop(tmp_path, capsys):
    _write_report_json("", {"x": 1})   # 예외·출력 없이 조용히 지나가야 한다
    assert capsys.readouterr().err == ""
