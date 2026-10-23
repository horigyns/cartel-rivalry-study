#!/usr/bin/env python3
"""Post-freeze recompute over existing pickles (no episode rerun, no threshold
change). Written alongside analysis.py so both can be reported.

  1. Defection rates with metrics.defections (corrected: bloc-instructed sides
     only) beside metrics.defections_legacy (frozen: both sides everywhere).
  2. Fisher's exact test on episode-level defection counts, each treatment
     cell against the counterfactual cell in the same arm.
  3. Paired bootstrap of cell DIFFERENCES (10,000 draws, episodes resampled
     within cell) for every pairwise channel contrast on mean position and
     median round closed, with a separated / not separated verdict.
  4. Headline 2x2 rebuilt on the corrected baseline, legacy beside it.
  5. MATCHED-denominator cohesion test (metrics.defections_matched): sellers
     only in every cell, so the counterfactual and treatment cells count the
     same number of bloc-instructed agents. defections() still compares 2
     agents against 4, which inflates treatment rates mechanically; this is
     the comparison the cohesion claim should rest on.
  6. Minimum detectable effect for that matched test (exact Fisher power), so
     a null states which effects it excluded rather than implying none exist.
  7. Continuous partner gap (metrics.partner_gap_mean), the fourth and final
     cohesion specification: no tolerance, exposure-normalised, defined on
     every episode with a scored round. PRIMARY test is pooled (all treatment
     episodes vs counterfactual, one comparison, no multiplicity); the
     per-cell breakdown is secondary, shares one control, and carries Holm.
     EXPLORATORY throughout - see PREREG.md, Deviations.

    python analysis_corrected.py
    -> results/analysis_corrected.json, results/cells_corrected.txt
"""
from __future__ import annotations

import json
import pickle
import random
import statistics as stats
import sys
from itertools import combinations
from pathlib import Path

from scipy.stats import fisher_exact, mannwhitneyu

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import metrics  # noqa: E402
import run as R  # noqa: E402

ARMS = {"accept-only": HERE / "runs", "seller-ask": HERE / "runs_ap"}
CHAN = ["ch-none_neutral", "ch-public_neutral", "ch-private_neutral"]
TREAT = CHAN + ["ch-private_neutral_full", "ch-private_unregulated"]
CF = "ch-private_neutral_cf"
B = 10_000
rng = random.Random(0)
OUT = HERE / "results"


def load(d: Path):
    outs = [pickle.loads(p.read_bytes()) for p in sorted(d.glob("*.pkl"))]
    return [o for o in outs if o.error is None and "@" not in o.cell_name]


def boot_diff(a, b_, fn, n=B):
    est = sorted(fn([rng.choice(a) for _ in a]) - fn([rng.choice(b_) for _ in b_]) for _ in range(n))
    return est[int(0.025 * n)], est[int(0.975 * n)]


def boot_ci(vals, fn, n=2000):
    est = sorted(fn([rng.choice(vals) for _ in vals]) for _ in range(n))
    return est[int(0.025 * n)], est[int(0.975 * n)]


def verdict(ci):
    return "separated" if (ci[0] > 0 or ci[1] < 0) else "not separated"


def cohesion(rate, base):
    return "tighter" if rate < base - 0.1 else ("looser" if rate > base + 0.1 else "same")


def holm(ps):
    """Holm-Bonferroni adjusted p-values, order preserved. Applied only to the
    per-cell secondary tests, which ask whether cells differ from each other.
    The primary cohesion question is one pooled comparison and carries no
    correction: five correlated tests against a shared control are not five
    families, and Holm is conservative to the point of misleading when tests
    are positively dependent and concordant."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    out, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * ps[i])
        out[i] = min(1.0, running)
    return out


def _fisher_p(k1, n1, k2, n2):
    return fisher_exact([[k1, n1 - k1], [k2, n2 - k2]])[1]


def power_at(p1, n1, p0, n2, alpha=0.05):
    """Exact power of a two-tailed Fisher test for a true treatment rate p1
    against baseline p0, enumerating every (k1, k2) outcome. n here is small
    enough that enumeration beats simulation."""
    from math import comb
    b1 = [comb(n1, k) * p1 ** k * (1 - p1) ** (n1 - k) for k in range(n1 + 1)]
    b2 = [comb(n2, k) * p0 ** k * (1 - p0) ** (n2 - k) for k in range(n2 + 1)]
    return sum(
        b1[k1] * b2[k2]
        for k1 in range(n1 + 1)
        for k2 in range(n2 + 1)
        if _fisher_p(k1, n1, k2, n2) <= alpha
    )


def mde(n1, p0, n2, target=0.80, alpha=0.05):
    """Smallest detectable departure from p0 in each direction at `target`
    power. Returns (tighter_rate, looser_rate), None where unreachable at this
    n. A null result has to state which effects it excluded; this is that
    number."""
    grid = [i / 100 for i in range(101)]
    tighter = next((p for p in [q for q in grid if q <= p0][::-1]
                    if power_at(p, n1, p0, n2, alpha) >= target), None)
    looser = next((p for p in grid if p >= p0
                   and power_at(p, n1, p0, n2, alpha) >= target), None)
    return tighter, looser


report = {"note": "post-freeze recompute; no episode rerun; thresholds unchanged (PREREG.md Deviations)"}
lines = []
for arm, d in ARMS.items():
    outs = load(d)
    R.RUNS = d
    accidental = R.accidental_crossings()
    by = {}
    for o in outs:
        by.setdefault(o.cell_name, []).append(o)
    base = R.baselines_from(by)
    arm_rep = {"cells": {}, "fisher": {}, "contrasts": {}, "headline": {}}

    # --- per-cell table, corrected beside legacy ---
    lines.append(f"\n===== {arm}: per-cell, corrected defection (bloc-instructed sides only) vs legacy =====")
    lines.append(f"{'cell':24} {'n':>3} {'medRound':>8} {'pos':>6} {'sd':>6} {'defect_corr':>11} {'defect_legacy':>13}  class")
    rates = {}
    for cell, os_ in sorted(by.items()):
        pb, ref = R.baseline_for(cell, base)
        s = metrics.summarise(os_, pb, ref, accidental)          # uses corrected defections
        corr = [1 if metrics.defections(o) > 0 else 0 for o in os_]
        leg = [1 if metrics.defections_legacy(o) > 0 else 0 for o in os_]
        mat = [1 if metrics.defections_matched(o) > 0 else 0 for o in os_]
        rates[cell] = (stats.fmean(corr), stats.fmean(leg), corr, leg,
                       stats.fmean(mat), mat)
        lines.append(f"{cell:24} {len(os_):3} {s.median_round_closed:8.1f} {s.mean_price_pos:6.3f} {s.price_sd:6.3f} "
                     f"{stats.fmean(corr):11.2f} {stats.fmean(leg):13.2f}  {s.classification}")
        ci_c = boot_ci(corr, stats.fmean); ci_l = boot_ci(leg, stats.fmean)
        ci_m = boot_ci(mat, stats.fmean)
        arm_rep["cells"][cell] = {"n": len(os_), "defection_corrected": stats.fmean(corr), "defection_legacy": stats.fmean(leg),
                                  "defection_matched": stats.fmean(mat),
                                  "ci_defect_corrected": ci_c, "ci_defect_legacy": ci_l, "ci_defect_matched": ci_m,
                                  "median_round": s.median_round_closed, "mean_pos": s.mean_price_pos, "sd": s.price_sd,
                                  "classification": s.classification, "bloc_roles": sorted(metrics.bloc_roles(cell)),
                                  "matched_roles": sorted(metrics.MATCHED_ROLES)}

    # --- Fisher's exact: treatment vs counterfactual, episode-level defection ---
    lines.append(f"\n===== {arm}: Fisher's exact, episodes with >=1 defection, treatment vs counterfactual (corrected) =====")
    lines.append(f"{'cell':24} {'treat def/n':>12} {'cf def/n':>10} {'odds ratio':>11} {'p (2-tailed)':>13}   legacy p")
    cf_c, cf_l = rates[CF][2], rates[CF][3]
    for cell in TREAT:
        tc, tl = rates[cell][2], rates[cell][3]
        tab = [[sum(tc), len(tc) - sum(tc)], [sum(cf_c), len(cf_c) - sum(cf_c)]]
        orr, p = fisher_exact(tab)
        tab_l = [[sum(tl), len(tl) - sum(tl)], [sum(cf_l), len(cf_l) - sum(cf_l)]]
        _, p_l = fisher_exact(tab_l)
        lines.append(f"{cell:24} {sum(tc):>6}/{len(tc):<5} {sum(cf_c):>4}/{len(cf_c):<5} {orr:11.2f} {p:13.3f}   {p_l:.3f}")
        arm_rep["fisher"][cell] = {"table": tab, "odds_ratio": orr, "p": p, "legacy_table": tab_l, "legacy_p": p_l}

    # --- matched-denominator cohesion test (sellers only, every cell) ---
    # defections() counts 2 agents in the counterfactual and 4 in treatment
    # cells, so an episode-level rate is mechanically higher under treatment
    # regardless of any real effect. Sellers are bloc-instructed everywhere, so
    # restricting to them fixes the denominator by construction. This is the
    # test the cohesion claim rests on.
    lines.append(f"\n===== {arm}: MATCHED denominator, sellers only in every cell =====")
    lines.append(f"{'cell':24} {'def/n':>10} {'rate':>6} {'95% CI':>16} {'odds ratio':>11} {'p (2-tailed)':>13}")
    cf_m = rates[CF][5]
    lines.append(f"{CF + ' (baseline)':24} {sum(cf_m):>4}/{len(cf_m):<5} {stats.fmean(cf_m):6.2f} "
                 f"[{boot_ci(cf_m, stats.fmean)[0]:.2f}, {boot_ci(cf_m, stats.fmean)[1]:.2f}]")
    arm_rep["matched"] = {"baseline": stats.fmean(cf_m), "baseline_n": len(cf_m), "cells": {}}
    for cell in TREAT:
        tm = rates[cell][5]
        tab_m = [[sum(tm), len(tm) - sum(tm)], [sum(cf_m), len(cf_m) - sum(cf_m)]]
        orr_m, p_m = fisher_exact(tab_m)
        ci_m = boot_ci(tm, stats.fmean)
        lines.append(f"{cell:24} {sum(tm):>4}/{len(tm):<5} {stats.fmean(tm):6.2f} "
                     f"[{ci_m[0]:.2f}, {ci_m[1]:.2f}] {orr_m:11.2f} {p_m:13.3f}")
        arm_rep["matched"]["cells"][cell] = {
            "rate": stats.fmean(tm), "n": len(tm), "ci": ci_m,
            "table": tab_m, "odds_ratio": orr_m, "p": p_m,
            "cohesion": cohesion(stats.fmean(tm), stats.fmean(cf_m)),
            "significant_at_05": bool(p_m <= 0.05),
        }

    # --- minimum detectable effect on the matched test ---
    # What a null excludes. Reported per treatment n against the cf baseline.
    lines.append(f"\n===== {arm}: minimum detectable effect, matched test (Fisher, alpha=0.05, power=0.80) =====")
    p0 = stats.fmean(cf_m)
    lines.append(f"baseline {p0:.2f} at n={len(cf_m)}; a true treatment rate must reach these to be detectable:")
    arm_rep["matched"]["mde"] = {}
    for n1 in sorted({len(rates[c][5]) for c in TREAT}):
        t, l = mde(n1, p0, len(cf_m))
        lines.append(f"  n={n1:<3} tighter than {('%.2f' % t) if t is not None else 'unreachable':>12}"
                     f"   looser than {('%.2f' % l) if l is not None else 'unreachable':>12}")
        arm_rep["matched"]["mde"][n1] = {"tighter_than": t, "looser_than": l}

    # --- continuous cohesion: partner gap (EXPLORATORY, 4th specification) ---
    # The three threshold-based measures each failed validation. This one has
    # no tolerance, is exposure-normalised, and is defined on every episode
    # with a scored round. The PRIMARY test is pooled: all treatment episodes
    # against the counterfactual, one comparison, no multiplicity. The per-cell
    # breakdown is secondary, answers a different question (do cells differ
    # from EACH OTHER), shares one control, and carries Holm.
    lines.append(f"\n===== {arm}: continuous partner gap, sellers only (EXPLORATORY - 4th specification) =====")
    gaps = {}
    for cell, os_ in sorted(by.items()):
        g = [v for o in os_ if (v := metrics.partner_gap_mean(o)) is not None]
        gaps[cell] = g
    cf_g = gaps.get(CF, [])
    n_excluded = sum(len(by[c]) - len(gaps[c]) for c in by)
    lines.append(f"episodes with >=1 scored round: {sum(len(v) for v in gaps.values())} "
                 f"of {sum(len(v) for v in by.values())} ({n_excluded} closed in round 1 and are excluded; "
                 f"the counterfactual loses {len(by[CF]) - len(cf_g)}, so the exclusion biases AGAINST tighter cohesion)")
    lines.append(f"{'cell':24} {'n':>3} {'q1':>7} {'median':>7} {'q3':>7} {'mean':>7} {'rank-bis':>9} {'p':>7} {'p Holm':>7}")
    arm_rep["gap"] = {"excluded_round1": n_excluded, "cells": {}}

    def _q(v):
        return stats.quantiles(v, n=4) if len(v) > 1 else [v[0], v[0], v[0]]

    def _rb_p(treat, ref):
        if len(treat) < 2 or len(ref) < 2:
            return None, None
        u, p = mannwhitneyu(treat, ref, alternative="two-sided")
        return 2 * u / (len(treat) * len(ref)) - 1, p

    per_cell = {}
    for cell in TREAT:
        g = gaps.get(cell, [])
        if not g:
            continue
        rb, p = _rb_p(g, cf_g)
        per_cell[cell] = (g, rb, p)
    adj = holm([per_cell[c][2] for c in per_cell])
    q = _q(cf_g)
    lines.append(f"{CF + ' (reference)':24} {len(cf_g):3} {q[0]:7.4f} {q[1]:7.4f} {q[2]:7.4f} {stats.fmean(cf_g):7.4f}")
    arm_rep["gap"]["cells"][CF] = {"n": len(cf_g), "median": q[1], "mean": stats.fmean(cf_g), "reference": True}
    for (cell, (g, rb, p)), pa in zip(per_cell.items(), adj):
        q = _q(g)
        lines.append(f"{cell:24} {len(g):3} {q[0]:7.4f} {q[1]:7.4f} {q[2]:7.4f} {stats.fmean(g):7.4f} "
                     f"{rb:9.2f} {p:7.3f} {pa:7.3f}")
        arm_rep["gap"]["cells"][cell] = {"n": len(g), "q1": q[0], "median": q[1], "q3": q[2],
                                         "mean": stats.fmean(g), "rank_biserial": rb,
                                         "p_raw": p, "p_holm": pa, "reference": False}

    # PRIMARY: pooled treatment vs counterfactual. One test, no correction.
    pooled = [v for c in TREAT for v in gaps.get(c, [])]
    rb_p, p_p = _rb_p(pooled, cf_g)
    lines.append(f"\n  POOLED (primary): all treatment episodes vs counterfactual, one test, no multiplicity")
    if rb_p is not None:
        qp = _q(pooled)
        lines.append(f"  treatment n={len(pooled)} median={qp[1]:.4f} mean={stats.fmean(pooled):.4f} | "
                     f"counterfactual n={len(cf_g)} median={_q(cf_g)[1]:.4f} mean={stats.fmean(cf_g):.4f}")
        lines.append(f"  rank-biserial {rb_p:+.2f}   Mann-Whitney p = {p_p:.4f}   "
                     f"(negative = bloc-instructed sellers diverge LESS)")
        arm_rep["gap"]["pooled"] = {"n_treatment": len(pooled), "n_reference": len(cf_g),
                                    "median_treatment": qp[1], "median_reference": _q(cf_g)[1],
                                    "mean_treatment": stats.fmean(pooled), "mean_reference": stats.fmean(cf_g),
                                    "rank_biserial": rb_p, "p": p_p}
    lines.append("  EXPLORATORY. Fourth cohesion specification; chosen on the structural grounds above")
    lines.append("  from diagnostics that preceded this result, not because it returned a preferred answer.")
    lines.append("  Not confirmatory. A replication needs this measure pre-registered.")
    if arm == "seller-ask":
        lines.append("  CONFOUNDED IN THIS ARM: AgenticPay sellers quote each buyer separately, so the gap")
        lines.append("  mixes cross-buyer price discrimination with any break from a shared line. Direction only.")

    # --- paired bootstrap contrasts between channel cells ---
    lines.append(f"\n===== {arm}: paired bootstrap, channel contrasts (10,000 draws, episodes resampled within cell) =====")
    lines.append(f"{'contrast':26} {'measure':>14} {'diff':>7} {'95% CI':>18}  verdict")
    for a_, b_ in combinations(CHAN, 2):
        A = [o for o in by[a_] if o.deal and (o.cell_name, o.seed) not in accidental]
        Bc = [o for o in by[b_] if o.deal and (o.cell_name, o.seed) not in accidental]
        for measure, key, fn in (("mean position", "normalised_price", stats.fmean), ("median round", "round_closed", stats.median)):
            va, vb = [getattr(o, key) for o in A], [getattr(o, key) for o in Bc]
            diff = fn(va) - fn(vb); ci = boot_diff(va, vb, fn)
            name = f"{a_.split('_')[0][3:]} - {b_.split('_')[0][3:]}"
            lines.append(f"{name:26} {measure:>14} {diff:+7.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}]  {verdict(ci)}")
            arm_rep["contrasts"][f"{name} | {measure}"] = {"diff": diff, "ci": ci, "verdict": verdict(ci)}

    # --- headline 2x2, corrected vs legacy baseline ---
    lines.append(f"\n===== {arm}: headline 2x2, corrected baseline vs legacy =====")
    bc, bl, bm = rates[CF][0], rates[CF][1], rates[CF][4]
    lines.append(f"counterfactual defection baseline: legacy {bl:.2f} (both sides, unmatched n) | "
                 f"corrected {bc:.2f} (bloc sides, unmatched n) | matched {bm:.2f} (sellers only, matched n)")
    lines.append(f"{'cell':24} {'outcome':22} {'legacy':>7} {'->':>8} {'corr':>7} {'->':>8} {'matched':>8} {'->':>8} {'p':>7}")
    for cell in TREAT:
        rc, rl, rm = rates[cell][0], rates[cell][1], rates[cell][4]
        vc, vl, vm = cohesion(rc, bc), cohesion(rl, bl), cohesion(rm, bm)
        pm = arm_rep["matched"]["cells"][cell]["p"]
        cls = arm_rep["cells"][cell]["classification"]
        lines.append(f"{cell:24} {cls:22} {rl:7.2f} {vl:>8} {rc:7.2f} {vc:>8} {rm:8.2f} {vm:>8} {pm:7.3f}")
        arm_rep["headline"][cell] = {"outcome": cls, "corrected_rate": rc, "corrected_verdict": vc,
                                     "legacy_rate": rl, "legacy_verdict": vl, "changed": vc != vl,
                                     "matched_rate": rm, "matched_verdict": vm, "matched_p": pm,
                                     "changed_corrected_to_matched": vc != vm}
    lines.append("The matched column is the one to report: it is the only comparison with an")
    lines.append("identical denominator on both sides. Legacy and corrected are shown for provenance.")
    arm_rep["baseline"] = {"corrected": bc, "legacy": bl, "matched": bm}
    report[arm] = arm_rep

# --- diff summary ---
lines.append("\n===== Diff summary =====")
for arm in ARMS:
    b = report[arm]["baseline"]
    lines.append(f"{arm}: counterfactual baseline legacy {b['legacy']:.2f} -> corrected {b['corrected']:.2f} -> matched {b['matched']:.2f}")
    ch = [c for c, v in report[arm]["headline"].items() if v["changed"]]
    lines.append(f"  cohesion verdicts changed (legacy -> corrected): {', '.join(f'{c} ({report[arm]['headline'][c]['legacy_verdict']} -> {report[arm]['headline'][c]['corrected_verdict']})' for c in ch) or 'none'}")
    ch2 = [c for c, v in report[arm]["headline"].items() if v["changed_corrected_to_matched"]]
    lines.append(f"  cohesion verdicts changed (corrected -> matched): {', '.join(f'{c} ({report[arm]['headline'][c]['corrected_verdict']} -> {report[arm]['headline'][c]['matched_verdict']})' for c in ch2) or 'none'}")
    sig = [c for c, v in report[arm]["matched"]["cells"].items() if v["significant_at_05"]]
    lines.append(f"  matched test significant at p<=0.05: {', '.join(sig) or 'none'}")
    lines.append(f"  cell n: {', '.join(f'{c}={v['n']}' for c, v in sorted(report[arm]['cells'].items()))}")

text = "\n".join(lines)
print(text)
(OUT / "analysis_corrected.json").write_text(json.dumps(report, indent=1, default=str))
(OUT / "cells_corrected.txt").write_text(text + "\n")
print(f"\nwrote {OUT / 'analysis_corrected.json'} and {OUT / 'cells_corrected.txt'}")
