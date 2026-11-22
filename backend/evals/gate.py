"""The eval gate (specs/evals.md §6): compare a results file with the accepted baseline.

    python evals/gate.py evals/results/real-....json evals/baselines/real.json [--summary $GITHUB_STEP_SUMMARY]

Exits 1 if any metric moved the wrong way by more than its tolerance in evals/gate.json, or if a
metric the baseline has is missing. Better scores never move the baseline by themselves: commit the
new baseline (copy the results' summary) so every change of the bar is reviewed.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def compare(result: dict[str, Any], baseline: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    rules = json.loads((HERE / "gate.json").read_text())["metrics"]
    rows = []
    for metric, rule in rules.items():
        if mode not in rule.get("modes", ["offline", "real"]):
            continue
        base, now = baseline.get(metric), result.get(metric)
        if base is None:
            continue  # not measured in the baseline: nothing to hold it to yet
        if now is None:
            rows.append({"metric": metric, "baseline": base, "now": None, "ok": False, "why": "missing"})
            continue
        if rule["better"] == "higher":
            floor = base - rule.get("tolerance", 0.0)
            ok, why = now >= floor - 1e-9, f">= {floor:.3f}"
        else:
            ceiling = base * (1 + rule["relative"]) if "relative" in rule else base + rule.get("tolerance", 0.0)
            ok, why = now <= ceiling + 1e-9, f"<= {ceiling:.3f}"
        rows.append({"metric": metric, "baseline": base, "now": now, "ok": ok, "why": why})
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    lines = ["| Metric | Baseline | Now | Rule | |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['metric']} | {r['baseline']} | {r['now']} | {r['why']} | {'✅' if r['ok'] else '❌'} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--summary", type=Path, help="append the table here (GitHub step summary)")
    args = parser.parse_args()
    report = json.loads(args.results.read_text())
    baseline = json.loads(args.baseline.read_text())
    rows = compare(report["summary"], baseline["summary"], report["mode"])
    failed = [r for r in rows if not r["ok"]]
    text = f"### Eval gate ({report['mode']}): {'FAILED' if failed else 'passed'}\n\n{table(rows)}\n"
    print(text)
    if args.summary:
        with args.summary.open("a") as fh:
            fh.write(text)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
