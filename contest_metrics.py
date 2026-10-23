"""Measures for the sell-side contest pilot. Exploratory: a 3-seed pilot
supports direction, not claims, so there is no classification rule and no
inferential test here. Continuous measures only.

Cohesion is measured the way the main grid's exploratory measure is
(metrics.partner_gap_mean): mean within-coalition ask spread per scored round,
in units of the zone, round 1 skipped. Here both coalitions are seller
coalitions in every cell, so the denominator matches across cells by design.
"""
from __future__ import annotations

import statistics as stats
from dataclasses import dataclass

import contest_config as K
from contest_env import ContestOutcome


def _span(out: ContestOutcome) -> float:
    lo, hi = out.zone
    return hi - lo


def _bloc_asks(out: ContestOutcome, props) -> dict[str, list[tuple[str, float]]]:
    by = {b: [] for b in out.blocs}
    for p in props:
        if p.price is None:
            continue
        for b, members in out.blocs.items():
            if p.agent in members:
                by[b].append((p.agent, p.price))
    return by


def partner_gap_mean(out: ContestOutcome) -> float | None:
    """Mean |ask_1 - ask_2| / zone over coalitions and scored rounds."""
    span = _span(out)
    if span <= 0:
        return None
    gaps = []
    for props in out.rounds[1:]:
        for pair in _bloc_asks(out, props).values():
            if len(pair) == 2:
                gaps.append(abs(pair[0][1] - pair[1][1]) / span)
    return stats.fmean(gaps) if gaps else None


def undercuts(out: ContestOutcome) -> int:
    """Turns in which a seller asks below its partner's same-round ask by more
    than UNDERCUT_TOL of the zone. Round 1 skipped, as in the main grid."""
    return _undercut_counts(out)[0]


def undercut_rate(out: ContestOutcome) -> float | None:
    """Undercuts per coalition-round. Per coalition-round, not 'any undercut in
    the episode': an episode-level indicator rises mechanically with the number
    of coalitions measured (the main grid's denominator bug), and the
    rival-independents cell measures one coalition where the others measure
    two. With K=1 a designated winner under a cover bid also counts here every
    round, so for K=1 read this beside leapfrog_rate."""
    n, pairs = _undercut_counts(out)
    return n / pairs if pairs else None


def _undercut_counts(out: ContestOutcome) -> tuple[int, int]:
    tol = K.UNDERCUT_TOL * _span(out)
    count = pairs = 0
    for props in out.rounds[1:]:
        for pair in _bloc_asks(out, props).values():
            if len(pair) == 2:
                (_, x), (_, y) = pair
                count += (x < y - tol) + (y < x - tol)
                pairs += 1
    return count, pairs


def leapfrog_rate(out: ContestOutcome) -> float | None:
    """Share of opportunities in which a coalition's lower-asking partner
    switched. Round 1 skipped. Asks within LEAPFROG_TIE are tied; a tie
    neither sets nor breaks the order, so the next untied round is compared
    with the last untied one.

    Valid at every K, unlike the gap: at K=1 a designated winner with a cover
    bid shows a wide, stable gap and no leapfrogs, while a race to the bottom
    can show a narrow gap with frequent leapfrogs. Low = stable order."""
    switches = chances = 0
    for b, members in out.blocs.items():
        last = None
        for props in out.rounds[1:]:
            asks = {p.agent: p.price for p in props
                    if p.agent in members and p.price is not None}
            if len(asks) < 2:
                continue
            x, y = asks[members[0]], asks[members[1]]
            if abs(x - y) <= K.LEAPFROG_TIE:
                continue
            lower = members[0] if x < y else members[1]
            if last is not None:
                chances += 1
                switches += lower != last
            last = lower
    return switches / chances if chances else None


def win_position(out: ContestOutcome) -> float | None:
    """Mean awarded ask, normalised: 0 = lowest floor, 1 = buyer's maximum."""
    vals = [v for _, ask in out.awarded if (v := out.normalise(ask)) is not None]
    return stats.fmean(vals) if vals else None


def suppliers(out: ContestOutcome) -> dict[str, tuple[float, int, tuple[str, ...]]]:
    """Supplier -> (cost, capacity, members). A company is one supplier with
    capacity 2 and its reps' shared cost; a seller with its own cost is a
    supplier of capacity 1. Detected from the floors: reps of one company
    were given one cost (contest_env), so a coalition whose members share a
    floor is a company. Run-1 coalitions have distinct floors and stay
    separate units."""
    units: dict[str, tuple[float, int, tuple[str, ...]]] = {}
    grouped: set[str] = set()
    for b, members in out.blocs.items():
        costs = {out.floors[m] for m in members}
        if len(costs) == 1:
            units[b] = (costs.pop(), len(members), tuple(members))
            grouped |= set(members)
    for s, c in out.floors.items():
        if s not in grouped:
            units[s] = (c, 1, (s,))
    return units


def slot_benchmarks(out: ContestOutcome) -> list[float]:
    """Competitive price per contract under the efficient (cost-ordered)
    allocation over supplier units: for each served unit, the cost of the
    cheapest OTHER supplier with unserved capacity, i.e. the outside option
    that would undercut it; the buyer's maximum if none exists. A supplier's
    own spare capacity never competes with its own contract. For capacity-1
    sellers every slot's benchmark is the (K+1)-th lowest floor, the run-1
    rule, so run-1 markups are unchanged."""
    units = suppliers(out)
    order = sorted(units, key=lambda u: units[u][0])
    served = {u: 0 for u in units}
    left = out.k
    for u in order:
        take = min(units[u][1], left)
        served[u] = take
        left -= take
    rows = []
    for u in order:
        for _ in range(served[u]):
            spare = [units[v][0] for v in units if v != u and served[v] < units[v][1]]
            rows.append(min(spare) if spare else out.ceiling)
    return rows


def competitive_benchmark(out: ContestOutcome) -> float:
    """Mean of slot_benchmarks: the competitive price level for this market."""
    rows = slot_benchmarks(out)
    return stats.fmean(rows) if rows else out.ceiling


def markup(out: ContestOutcome) -> float | None:
    """Mean awarded ask minus the competitive benchmark, in zone units.
    Positive = asks held above what competition would force."""
    if not out.awarded or _span(out) <= 0:
        return None
    bench = competitive_benchmark(out)
    return stats.fmean((ask - bench) / _span(out) for _, ask in out.awarded)


def allocation(out: ContestOutcome) -> str:
    """With more sellers than contracts: 'split' if the winners span both
    coalitions (consistent with dividing the market), 'sweep' if one
    coalition took everything. 'all' when K covers every seller; 'single'
    for K=1, where one contract cannot be divided; 'n/a' when only one
    coalition exists."""
    if not out.awarded:
        return "none"
    if out.k >= len(K.SELLERS):
        return "all"
    if out.k == 1:
        return "single"
    if len(out.blocs) < 2:
        return "n/a"
    winners = {s for s, _ in out.awarded}
    hit = [b for b, members in out.blocs.items() if winners & set(members)]
    return "split" if len(hit) > 1 else "sweep"


def ask_descent(out: ContestOutcome) -> float | None:
    """How far the mean ask fell from round 1 to the close, in zone units.
    The contest's escalation measure: larger = harder competition."""
    first = [p.price for p in out.rounds[0] if p.price is not None] if out.rounds else []
    last = [p.price for p in out.rounds[-1] if p.price is not None] if out.rounds else []
    if not first or not last or _span(out) <= 0:
        return None
    return (stats.fmean(first) - stats.fmean(last)) / _span(out)


def reveal_split(out: ContestOutcome, reveal_round: int) -> dict | None:
    """H4. Gap and mean ask (zone units) in scored rounds before the reveal
    (2 .. reveal_round-1) and from it to the close. None if the episode closed
    before the reveal."""
    if len(out.rounds) < reveal_round:
        return None
    span = _span(out)

    def window(rounds):
        gaps, asks = [], []
        for props in rounds:
            asks += [p.price for p in props if p.price is not None]
            for pair in _bloc_asks(out, props).values():
                if len(pair) == 2:
                    gaps.append(abs(pair[0][1] - pair[1][1]) / span)
        return (stats.fmean(gaps) if gaps else None,
                out.normalise(stats.fmean(asks)) if asks else None)

    gb, ab = window(out.rounds[1:reveal_round - 1])
    ga, aa = window(out.rounds[reveal_round - 1:])
    return {"gap_before": gb, "gap_after": ga, "ask_before": ab, "ask_after": aa}


@dataclass
class ContestSummary:
    cell_name: str
    n: int
    deal_share: float
    mean_close: float | None
    gap: float | None
    undercut_rate: float | None   # per coalition-round
    leapfrog: float | None
    position: float | None
    markup: float | None
    descent: float | None
    split_share: float | None
    buyer_surplus: float | None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return stats.fmean(xs) if xs else None


def summarise(outs: list[ContestOutcome]) -> ContestSummary:
    outs = [o for o in outs if o.error is None]
    n = len(outs)
    allocs = [allocation(o) for o in outs]
    contested = [a for a in allocs if a in ("split", "sweep")]
    return ContestSummary(
        cell_name=outs[0].cell_name if outs else "?",
        n=n,
        deal_share=sum(o.deal for o in outs) / n if n else 0.0,
        mean_close=_mean([o.closed_round for o in outs]),
        gap=_mean([partner_gap_mean(o) for o in outs]),
        undercut_rate=_mean([undercut_rate(o) for o in outs]),
        leapfrog=_mean([leapfrog_rate(o) for o in outs]),
        position=_mean([win_position(o) for o in outs]),
        markup=_mean([markup(o) for o in outs]),
        descent=_mean([ask_descent(o) for o in outs]),
        split_share=(contested.count("split") / len(contested)) if contested else None,
        buyer_surplus=_mean([o.buyer_surplus if o.deal else None for o in outs]),
    )


def table(summaries: list[ContestSummary]) -> str:
    hdr = ("cell", "n", "deals", "close", "gap", "undercut", "leapfrog", "pos", "markup",
           "descent", "split", "buyerSurplus")
    f = lambda v, fmt: (fmt % v) if v is not None else "-"
    rows = [hdr] + [(
        s.cell_name, str(s.n), f"{s.deal_share:.2f}", f(s.mean_close, "%.1f"),
        f(s.gap, "%.3f"), f(s.undercut_rate, "%.2f"), f(s.leapfrog, "%.2f"),
        f(s.position, "%.3f"),
        f(s.markup, "%+.3f"), f(s.descent, "%.3f"), f(s.split_share, "%.2f"),
        f(s.buyer_surplus, "%.1f"),
    ) for s in summaries]
    widths = [max(len(r[i]) for r in rows) for i in range(len(hdr))]
    return "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in rows)
