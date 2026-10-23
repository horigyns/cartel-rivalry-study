#!/usr/bin/env python3
"""Inferential layer over both grids. Reads runs/ and runs_ap/ pickles and the
reasoning_judge.json files if present. No model calls.

    python analysis.py            # prints tables, writes results/analysis.json

Everything here is descriptive with bootstrap CIs; 10 to 18 seeds per cell
supports directional claims, not effect sizes (PREREG.md, Analysis plan).
"""
from __future__ import annotations

import json
import pickle
import random
import statistics as stats
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import config as C  # noqa: E402
import metrics  # noqa: E402
from prompts import paraphrase_of  # noqa: E402
from run import accidental_crossings, baselines_from, baseline_for  # noqa: E402

ARMS = {"accept-only": HERE / "runs", "seller-ask": HERE / "runs_ap"}
OUT = HERE / "results"
OUT.mkdir(exist_ok=True)
B = 2000
rng = random.Random(0)


def load(d: Path):
    outs = [pickle.loads(p.read_bytes()) for p in sorted(d.glob("*.pkl"))]
    return [o for o in outs if o.error is None and "@" not in o.cell_name]


def boot_ci(vals, fn, b=B):
    if len(vals) < 2:
        return (None, None)
    est = [fn([rng.choice(vals) for _ in vals]) for _ in range(b)]
    est.sort()
    return est[int(0.025 * b)], est[int(0.975 * b)]


def fmt_ci(ci, f="%.2f"):
    return "-" if ci[0] is None else f"[{f % ci[0]}, {f % ci[1]}]"


def cell_rows(outs, accidental):
    rows = {}
    for o in outs:
        rows.setdefault(o.cell_name, []).append(o)
    return rows


def main():
    report = {}
    per_arm_cells = {}
    for arm, d in ARMS.items():
        outs = load(d)
        import run as R
        R.RUNS = d                                  # accidental_crossings reads RUNS
        accidental = accidental_crossings()
        by_cell = cell_rows(outs, accidental)
        base = baselines_from(by_cell)
        print(f"\n===== {arm} ({len(outs)} episodes, {len(accidental)} accidental crossings) =====")
        print(f"{'cell':24} {'n':>3} {'median round [95% CI]':>24} {'mean position [95% CI]':>26} {'SD':>6} {'defect [95% CI]':>20}  class")
        per_arm_cells[arm] = {}
        for cell, os_ in sorted(by_cell.items()):
            payoff_base, ref = baseline_for(cell, base)
            s = metrics.summarise(os_, payoff_base, ref, accidental)
            deals = [o for o in os_ if o.deal and (o.cell_name, o.seed) not in accidental]
            rounds = [o.round_closed for o in deals]
            pos = [o.normalised_price for o in deals]
            defect = [1.0 if metrics.defections(o) > 0 else 0.0 for o in os_]
            ci_r = boot_ci(rounds, stats.median); ci_p = boot_ci(pos, stats.fmean); ci_d = boot_ci(defect, stats.fmean)
            print(f"{cell:24} {len(os_):3} {s.median_round_closed:5.1f} {fmt_ci(ci_r, '%.1f'):>18} {s.mean_price_pos:7.3f} {fmt_ci(ci_p, '%.3f'):>18} {s.price_sd:6.3f} {s.defection_rate:5.2f} {fmt_ci(ci_d):>14}  {s.classification}")
            per_arm_cells[arm][cell] = {"n": len(os_), "rounds": rounds, "pos": pos, "defect": defect,
                                        "summary": s.__dict__, "ci_round": ci_r, "ci_pos": ci_p, "ci_defect": ci_d}
        report[arm] = {c: {k: v for k, v in d_.items() if k not in ("rounds", "pos", "defect")} for c, d_ in per_arm_cells[arm].items()}

    # ---- microstructure vs channel: which factor moves the outcomes more? ----
    print("\n===== Factor comparison (minimal-bloc channel cells only) =====")
    chan_cells = ["ch-none_neutral", "ch-public_neutral", "ch-private_neutral"]
    def pooled(arm, key):
        return [v for c in chan_cells for v in per_arm_cells[arm][c][key]]
    for key, label, fn in (("rounds", "round closed (median)", stats.median), ("pos", "position (mean)", stats.fmean)):
        a, b_ = pooled("accept-only", key), pooled("seller-ask", key)
        micro = fn(b_) - fn(a)
        ci = boot_ci(list(zip(a + b_, [0] * len(a) + [1] * len(b_))),
                     lambda s: fn([v for v, g in s if g]) - fn([v for v, g in s if not g]))
        print(f"  microstructure effect on {label}: seller-ask minus accept-only = {micro:+.2f} {fmt_ci(ci)}")
        for arm in ARMS:
            vals = {c: fn(per_arm_cells[arm][c][key]) for c in chan_cells}
            rng_ = max(vals.values()) - min(vals.values())
            print(f"  channel range on {label} within {arm}: {rng_:.2f}  "
                  + ", ".join(f"{c.split('_')[0][3:]}={v:.2f}" for c, v in vals.items()))
        report[f"factor_{key}"] = {"microstructure_effect": micro, "ci": ci}

    # ---- pre-registered: channel ordering on time-to-deal within arm ----
    print("\n===== Pre-registered direction: median round none > public > private, within arm =====")
    for arm in ARMS:
        m = {c: stats.median(per_arm_cells[arm][c]["rounds"]) for c in chan_cells}
        ok = m["ch-none_neutral"] >= m["ch-public_neutral"] >= m["ch-private_neutral"]
        print(f"  {arm}: none={m['ch-none_neutral']:.1f} public={m['ch-public_neutral']:.1f} private={m['ch-private_neutral']:.1f}  -> {'consistent' if ok else 'not consistent'}")

    # ---- paraphrase stability (prereg secondary 5) ----
    print("\n===== Paraphrase stability: classification by paraphrase (k = seed mod 3) =====")
    for arm, d in ARMS.items():
        outs = load(d)
        import run as R
        R.RUNS = d
        accidental = accidental_crossings()
        by_cell = cell_rows(outs, accidental)
        base = baselines_from(by_cell)
        for cell, os_ in sorted(by_cell.items()):
            labels = []
            for k in range(3):
                sub = [o for o in os_ if paraphrase_of(o.seed) == k]
                payoff_base, ref = baseline_for(cell, base)
                labels.append(metrics.summarise(sub, payoff_base, ref, accidental).classification if len(sub) >= 3 else "n<3")
            flag = "" if len(set(labels)) == 1 else "  <-- varies"
            print(f"  {arm:11} {cell:24} " + " | ".join(f"k{k}:{l}" for k, l in enumerate(labels)) + flag)

    # ---- headline 2x2: inter-bloc outcome x intra-bloc cohesion vs counterfactual ----
    print("\n===== Headline: inter-bloc outcome x cohesion (defection rate vs no-rival baseline = cf cell) =====")
    for arm in ARMS:
        cf = per_arm_cells[arm]["ch-private_neutral_cf"]
        cf_def = stats.fmean(cf["defect"])
        for cell in ("ch-none_neutral", "ch-public_neutral", "ch-private_neutral", "ch-private_neutral_full", "ch-private_unregulated"):
            s = per_arm_cells[arm][cell]["summary"]
            d = stats.fmean(per_arm_cells[arm][cell]["defect"])
            coh = "tighter" if d < cf_def - 0.1 else ("looser" if d > cf_def + 0.1 else "same")
            print(f"  {arm:11} {cell:24} outcome={s['classification']:22} defect={d:.2f} vs cf {cf_def:.2f} -> cohesion {coh}")

    (OUT / "analysis.json").write_text(json.dumps(report, indent=1, default=str))
    print(f"\nwrote {OUT / 'analysis.json'}")


if __name__ == "__main__":
    main()
