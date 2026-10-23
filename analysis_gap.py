#!/usr/bin/env python3
"""Exploratory, post-freeze (PREREG.md Deviations). Continuous cohesion
measure: mean absolute seller-partner price gap per scored round, normalised
by zone span. Sellers only in every cell (the only side instructed as a bloc
in the counterfactual). Scored rounds are rounds 2..close, as for
metrics.defections. Episodes that closed in round 1 have no scored round and
are excluded and counted. No tolerance parameter; exposure-normalised by
construction (a per-round mean). Mann-Whitney U, two-sided, each treatment
cell against the counterfactual in the same arm; Holm across the five
comparisons per arm; rank-biserial effect size (positive = wider gaps than
the counterfactual, i.e. looser).

    python analysis_gap.py  -> results/analysis_gap.json, results/gap.txt
"""
from __future__ import annotations

import json
import pickle
import statistics as st
from pathlib import Path

from scipy.stats import mannwhitneyu

HERE = Path(__file__).parent
ARMS = {"accept-only": HERE / "runs", "seller-ask": HERE / "runs_ap"}
CF = "ch-private_neutral_cf"
TREAT = ["ch-none_neutral", "ch-public_neutral", "ch-private_neutral",
         "ch-private_neutral_full", "ch-private_unregulated"]


def mean_gap(o) -> float | None:
    lo, hi = o.zone
    span = hi - lo
    gaps = []
    for props in o.rounds[1:]:
        s = [p.price for p in props if p.role == "seller" and p.price is not None]
        if len(s) == 2:
            gaps.append(abs(s[0] - s[1]) / span)
    return st.fmean(gaps) if gaps else None


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, run = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = run
    return adj


report, lines = {"note": "exploratory, post-freeze; sellers only; no tolerance"}, []
for arm, d in ARMS.items():
    outs = [pickle.loads(p.read_bytes()) for p in sorted(d.glob("*.pkl"))]
    outs = [o for o in outs if o.error is None and "@" not in o.cell_name]
    by: dict[str, list] = {}
    for o in outs:
        by.setdefault(o.cell_name, []).append(o)
    vals = {c: [g for o in os_ if (g := mean_gap(o)) is not None] for c, os_ in by.items()}
    excl = {c: sum(mean_gap(o) is None for o in os_) for c, os_ in by.items()}
    cf = vals[CF]
    lines.append(f"\n===== {arm}: mean seller-partner gap per scored round / zone span =====")
    lines.append(f"{'cell':24} {'n':>3} {'excl r1':>7} {'median':>7} {'IQR':>15} {'mean':>6}")
    for c in [CF] + TREAT:
        v = vals[c]; q = st.quantiles(v, n=4) if len(v) >= 2 else [float("nan")] * 3
        lines.append(f"{c:24} {len(v):3} {excl[c]:7} {st.median(v):7.3f} [{q[0]:.3f}, {q[2]:.3f}] {st.fmean(v):6.3f}")
    raw = []
    for c in TREAT:
        u, p = mannwhitneyu(vals[c], cf, alternative="two-sided")
        rbc = 2 * u / (len(vals[c]) * len(cf)) - 1
        raw.append((c, u, p, rbc))
    adj = holm([p for _, _, p, _ in raw])
    lines.append(f"\n{'treatment vs cf':24} {'n':>7} {'U':>6} {'p':>7} {'p Holm':>7} {'rank-biserial':>14}  direction")
    report[arm] = {"cells": {c: {"n": len(vals[c]), "excluded_round1": excl[c], "median": st.median(vals[c]),
                                 "values": vals[c]} for c in vals}, "tests": {}}
    for (c, u, p, rbc), pa in zip(raw, adj):
        dirn = "looser" if rbc > 0 else "tighter"
        lines.append(f"{c:24} {len(vals[c]):>3}v{len(cf):<3} {u:6.1f} {p:7.3f} {pa:7.3f} {rbc:+14.2f}  {dirn}")
        report[arm]["tests"][c] = {"U": u, "p": p, "p_holm": pa, "rank_biserial": rbc, "direction": dirn}
    if arm == "seller-ask":
        lines.append("note: seller-ask quotes are per-buyer threads (price = seller's lowest thread quote);")
        lines.append("      the gap mixes cross-buyer price discrimination with any break from a line.")

text = "\n".join(lines)
print(text)
(HERE / "results/analysis_gap.json").write_text(json.dumps(report, indent=1))
(HERE / "results/gap.txt").write_text(text + "\n")
print("\nwrote results/analysis_gap.json and results/gap.txt")
