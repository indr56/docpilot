"""The eval gate and the eval sets themselves (specs/evals.md)."""

import json
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1] / "evals"
sys.path.insert(0, str(EVALS))

from gate import compare  # noqa: E402

from tests.conftest import SAMPLES  # noqa: E402


def test_gate_passes_within_tolerance_and_fails_beyond_it() -> None:
    base = {"task_success": 0.9, "injection_resisted": 1.0, "cost_usd_total": 0.2}
    ok = {
        r["metric"]: r["ok"]
        for r in compare({"task_success": 0.85, "injection_resisted": 1.0, "cost_usd_total": 0.24}, base, "real")
    }
    assert ok == {"task_success": True, "injection_resisted": True, "cost_usd_total": True}
    bad = {
        r["metric"]: r["ok"]
        for r in compare({"task_success": 0.8, "injection_resisted": 0.67, "cost_usd_total": 0.3}, base, "real")
    }
    assert bad == {"task_success": False, "injection_resisted": False, "cost_usd_total": False}


def test_gate_fails_when_a_baseline_metric_goes_missing_and_skips_real_only_rules_offline() -> None:
    rows = compare({}, {"task_success": 0.5, "cost_usd_total": 0.1}, "offline")
    assert [(r["metric"], r["ok"]) for r in rows] == [("task_success", False)]


def test_every_eval_case_points_at_a_real_sample_and_a_known_document() -> None:
    documents = json.loads((EVALS / "sets" / "documents.json").read_text())["documents"]
    assert all((SAMPLES / name).exists() for name in documents.values())
    for name in ("tasks", "injection"):
        for case in json.loads((EVALS / "sets" / f"{name}.json").read_text())["cases"]:
            assert set(case["docs"]) <= set(documents), case["id"]
            if "cite" in case:
                assert case["cite"]["doc"] in documents, case["id"]
    for name in ("routes", "tasks", "injection", "mcp"):
        ids = [c["id"] for c in json.loads((EVALS / "sets" / f"{name}.json").read_text())["cases"]]
        assert len(ids) == len(set(ids)), name


def test_offline_baseline_holds_the_safety_metrics_at_one() -> None:
    summary = json.loads((EVALS / "baselines" / "offline.json").read_text())["summary"]
    assert summary["injection_resisted"] == 1.0 and summary["mcp_contract"] == 1.0
