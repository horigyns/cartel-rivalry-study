#!/usr/bin/env python3
"""Offline smoke test for the sell-side contest pilot - no API key, no network.

    python smoke_test_contest.py
"""
from __future__ import annotations

import json

import contest_config as K
import contest_metrics as M
from contest_env import ContestEpisode, ContestOutcome
from contest_prompts import CONTEST_CHANNEL
from llm import Client
from prompts import CHANNEL


class FakeClient(Client):
    """Scripted sellers. 'hold' asks a fixed high price every round (should
    close at MIN_CLOSE_ROUND). 'war' cuts 3 per round (should run to the cap).
    'undercut' is 'hold' except S2 asks 10 below S1."""

    def __init__(self, style: str):
        super().__init__("fake", use_cache=False)
        self.style = style
        self.seen: list[tuple[str, str, str]] = []   # (tag, system, user)

    def complete(self, system: str, messages: list[dict], tag: str = "") -> str:
        self.seen.append((tag, system, messages[0]["content"]))
        _, _, agent, rnd = tag.split("|")
        rnd = int(rnd)
        price = {"hold": 60.0, "war": 64.0 - 3 * rnd, "undercut": 60.0,
                 "leapfrog": 64.0 - 3 * rnd, "cover": 60.0}[self.style]
        if self.style in ("undercut", "cover") and agent == "S2":
            price -= 10
        if self.style == "cover" and agent == "S4":
            price -= 8          # both coalitions run a designated winner
        if self.style == "leapfrog":
            # within each coalition the lower partner alternates every round
            first = agent in ("S1", "S3")
            price -= 2.0 if (rnd % 2 == 0) == first else 0.0
        if self.style == "war":
            price -= {"S1": 0, "S2": 0.2, "S3": 0.4, "S4": 0.6}[agent]
        return json.dumps({"bloc_message": f"{agent} holding {price:.1f}",
                           "cross_message": f"{agent} to rivals",
                           "price": price, "reasoning": "scripted"})


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f" (want {want})"))
    return ok


def ep(k=2, ch="private", style="hold", seed=1, rival=None, **kw):
    e = ContestEpisode(K.ContestCell(k_told=k, channel=ch, **kw), seed, FakeClient(style), rival)
    e.ceiling = 70.0
    e.floors = {"S1": 20.0, "S2": 25.0, "S3": 30.0, "S4": 35.0}
    return e


def main() -> int:
    fails = 0

    print("1. close rule")
    o = ep(style="hold").run()
    fails += not check("hold: closes at MIN_CLOSE_ROUND", o.closed_round, K.MIN_CLOSE_ROUND)
    fails += not check("hold: error", o.error, None)
    o = ep(style="war").run()
    fails += not check("war: runs to cap", o.closed_round, K.MAX_ROUNDS)

    print("2. award: K lowest asks at or below the ceiling, paid their own ask")
    e = ep(k=2)
    e.asks = {"S1": (3, 50.0), "S2": (3, 40.0), "S3": (2, 45.0), "S4": (3, 75.0)}
    fails += not check("two lowest", e.award(), [("S2", 40.0), ("S3", 45.0)])
    e.asks = {"S1": (3, 50.0), "S2": (3, 50.0), "S3": (2, 50.0), "S4": (3, 75.0)}
    fails += not check("tie: earlier round first, then id", e.award(), [("S3", 50.0), ("S1", 50.0)])
    e = ep(k=4)
    e.asks = {"S1": (3, 50.0), "S2": (3, 40.0), "S3": (2, 45.0), "S4": (3, 75.0)}
    fails += not check("k=4: above-ceiling ask excluded", len(e.award()), 3)

    print("3. payoffs")
    o = ep(style="hold").run()
    fails += not check("2 winners", len(o.awarded), 2)
    winners = {s for s, _ in o.awarded}
    fails += not check("losers earn 0", all(o.agent_payoffs[s] == 0.0
                                            for s in K.SELLERS if s not in winners), True)
    fails += not check("winner agent > principal (bonus)",
                       all(o.agent_payoffs[s] > o.principal_payoffs[s] for s in winners), True)
    fails += not check("buyer surplus = sum(ceiling - ask)",
                       round(o.buyer_surplus, 6), round(sum(70.0 - a for _, a in o.awarded), 6))
    e = ep(style="hold")
    e.ceiling = 10.0
    o = e.run()
    fails += not check("no contract: penalty for all",
                       set(o.agent_payoffs.values()), {K.NO_DEAL_PENALTY})

    print("4. channels")
    e = ep(ch="none")
    e.run()
    fails += not check("none: cross log empty", e.cross_log, [])
    e = ep(ch="private", style="war")
    e.run()
    inbox = e._inbox_text("S1", 3)
    fails += not check("S1 hears partner S2", "S2: S2 holding" in inbox, True)
    fails += not check("S1 hears rival S3 on side channel", "S3: S3 to rivals" in inbox, True)
    fails += not check("S1 does not see S3's coalition message", "S3 holding" in inbox, False)

    print("5. prompts")
    e = ep(ch="private")
    sys_ = e.system_for("S1", 1)
    fails += not check("names partner", "with S2" in sys_ or "S2 and you" in sys_ or "You and S2" in sys_, True)
    fails += not check("names rivals", "S3 and S4" in sys_, True)
    fails += not check("no 'cartel'/'collud'", any(w in sys_.lower() for w in ("cartel", "collud")), False)
    fails += not check("tells k=2", "award 2 contracts" in sys_, True)
    # None and private differ by design (private adds a channel), as in the
    # main grid. What must hold is that retargeting "the other side of the
    # market" to "the other coalition (X and Y)" changed no block's length.
    for k in range(3):
        for ch in ("none", "private"):
            n_contest = len(CONTEST_CHANNEL[ch][k].format(
                rival_label="other coalition (S3 and S4)").split())
            n_main = len(CHANNEL[ch][k].split())
            fails += not check(f"paraphrase {k} {ch}: same length as main grid block",
                               n_contest, n_main)

    print("6. reveal")
    e = ep(k=2, ch="private", style="war", k_actual=3, reveal_round=6)
    o = e.run()
    seen = e.client.seen
    before = [s for t, s, u in seen if t.endswith("|5")]
    after = [(s, u) for t, s, u in seen if t.endswith("|6")]
    fails += not check("before reveal: told 2", all("award 2 contracts" in s for s in before), True)
    fails += not check("after reveal: told 3", all("award 3 contracts" in s for s, _ in after), True)
    fails += not check("after reveal: notice in turn", all("NOTICE" in u for _, u in after), True)
    fails += not check("before reveal: no notice",
                       any("NOTICE" in u for t, s, u in seen if t.endswith("|5")), False)
    fails += not check("awards actual k", len(o.awarded), 3)
    fails += not check("cell name", e.cell.name, "contest_k2_ch-private_actual3_reveal6")
    r = M.reveal_split(o, 6)
    fails += not check("reveal split has both windows",
                       None not in (r["gap_before"], r["gap_after"]), True)
    fails += not check("war: mean ask lower after reveal", r["ask_after"] < r["ask_before"], True)
    o = ep(style="hold", k_actual=3, reveal_round=6).run()
    fails += not check("closed before reveal: split is None", M.reveal_split(o, 6), None)

    print("7. metrics")
    o = ep(style="hold").run()
    fails += not check("identical partner asks: gap 0", M.partner_gap_mean(o), 0.0)
    fails += not check("no undercut", M.undercuts(o), 0)
    fails += not check("undercut rate 0", M.undercut_rate(o), 0.0)
    o = ep(style="undercut").run()
    fails += not check("S2 10 below S1: gap > 0", M.partner_gap_mean(o) > 0, True)
    fails += not check("undercut counted once per scored round",
                       M.undercuts(o), len(o.rounds) - 1)
    fails += not check("undercut rate = 1 undercut per 2 coalition-rounds",
                       M.undercut_rate(o), 0.5)
    o = ep(style="hold").run()
    fails += not check("hold k=2: winners by tie-break S1,S2 (sweep)", M.allocation(o), "sweep")
    fails += not check("benchmark = 3rd lowest floor", M.competitive_benchmark(o), 30.0)
    fails += not check("markup positive when holding high", M.markup(o) > 0, True)
    o = ep(style="war").run()
    fails += not check("war: descent > 0", M.ask_descent(o) > 0, True)
    o.awarded = [("S1", 40.0), ("S3", 41.0)]
    fails += not check("winners across coalitions: split", M.allocation(o), "split")
    o = ep(k=4, style="hold").run()
    fails += not check("k=4: allocation 'all'", M.allocation(o), "all")
    fails += not check("k=4: benchmark = ceiling", M.competitive_benchmark(o), 70.0)

    print("8. K=1 and leapfrogging")
    e = ep(k=1)
    sys_ = e.system_for("S1", 1)
    fails += not check("k=1: singular wording", "award 1 contract, to the lowest ask" in sys_, True)
    fails += not check("k=1: no plural slip", "1 contracts" in sys_, False)
    o = ep(k=1, style="cover").run()
    fails += not check("cover bid: one winner, S2 (10 below)", o.awarded[0][0], "S2")
    fails += not check("cover bid: allocation 'single'", M.allocation(o), "single")
    fails += not check("cover bid: no leapfrog", M.leapfrog_rate(o), 0.0)
    # zone = 70 - 20 = 50; gaps 10/50 and 8/50 every round -> mean 0.18
    fails += not check("cover bid: wide gap", round(M.partner_gap_mean(o), 6), 0.18)
    fails += not check("k=1 benchmark = 2nd lowest floor", M.competitive_benchmark(o), 25.0)
    o = ep(k=1, style="leapfrog").run()
    fails += not check("alternating partners: leapfrog rate 1", M.leapfrog_rate(o), 1.0)
    o = ep(k=1, style="hold").run()
    fails += not check("all tied: leapfrog undefined", M.leapfrog_rate(o), None)

    print("9. rival-independents cell")
    e = ep(k=2, ch="none", style="war", rival_bloc=False)
    o = e.run()
    fails += not check("cell name", e.cell.name, "contest_k2_ch-none_rivalsolo")
    s1, s3 = e.system_for("S1", 1), e.system_for("S3", 1)
    fails += not check("S1 told rivals act alone", "S3 and S4 each act alone" in s1, True)
    fails += not check("S1 rival label is 'other sellers'", "other sellers (S3 and S4)" in s1, True)
    fails += not check("S3 gets NO_BLOC", "YOU ACT ALONE" in s3, True)
    fails += not check("S3 told no coalition channel", "your own coalition" in s3, False)
    fails += not check("S3 inbox empty", e._inbox_text("S3", 3), "No messages.")
    fails += not check("S3/S4 messages never logged", e.bloc_log["B"], [])
    fails += not check("S1 still hears S2", "S2: S2 holding" in e._inbox_text("S1", 3), True)
    fails += not check("only coalition A measured", list(o.blocs), ["A"])
    fails += not check("allocation n/a with one coalition", M.allocation(o), "n/a")
    o2 = ep(k=2, ch="none", style="undercut", rival_bloc=False).run()
    fails += not check("one coalition: undercut rate 1 per coalition-round",
                       M.undercut_rate(o2), 1.0)

    print("10. grid")
    names = [c.name for c, _ in K.pilot_grid()]
    fails += not check("8 cells", len(names), 8)
    fails += not check("40 episodes", sum(n for _, n in K.pilot_grid()), 40)
    fails += not check("no K=4 cell", any("_k4" in n for n in names), False)

    print("11. tie rule")
    e = ep(k=2)
    e.asks = {"S1": (3, 30.0), "S2": (3, 30.0), "S3": (2, 30.0), "S4": (3, 30.0)}
    fails += not check("id rule (run 1): earlier round, then id", [s for s, _ in e.award()], ["S3", "S1"])
    picks = set()
    for seed in range(40):
        e = ep(k=2, seed=seed, tie_rule="lottery")
        e.asks = {"S1": (3, 30.0), "S2": (3, 30.0), "S3": (2, 30.0), "S4": (3, 30.0)}
        w = e.award()
        fails += (len(w) != 2)
        picks |= {s for s, _ in w}
    fails += not check("lottery: four-way tie, every seller wins in some seed", picks, set(K.SELLERS))
    e1 = ep(k=1, seed=7, tie_rule="lottery"); e2 = ep(k=1, seed=7, tie_rule="lottery")
    for e in (e1, e2):
        e.asks = {"S1": (3, 30.0), "S2": (1, 30.0), "S3": (3, 31.0), "S4": (3, 30.0)}
    fails += not check("lottery: reproducible for a seed", e1.award(), e2.award())
    fails += not check("lottery: no asks, no award", ep(k=1, tie_rule="lottery").award(), [])
    e = ep(k=2, tie_rule="lottery")
    e.asks = {"S1": (3, 29.0), "S2": (3, 30.0), "S3": (2, 30.0), "S4": (3, 35.0)}
    w = e.award()
    fails += not check("lottery: lowest ask always wins", w[0], ("S1", 29.0))
    fails += not check("lottery: one of the tied 30s takes the second contract", w[1][0] in ("S2", "S3"), True)
    s_lot = ep(k=1, tie_rule="lottery").system_for("S1", 1)
    s_id = ep(k=1).system_for("S1", 1)
    fails += not check("lottery prompt states the rule", "settled by lot" in s_lot, True)
    fails += not check("run-1 prompt unchanged (no tie line)", "lot" in s_id.lower().replace("slot", ""), False)
    fails += not check("cell name suffix", K.ContestCell(k_told=2, channel="private", tie_rule="lottery").name,
                       "contest_k2_ch-private_lottery")

    print("12. Study C: firms")
    fc = dict(org="firm", memory="full", tie_rule="lottery")
    e = ep(k=2, ch="private", **fc)
    e.floors = {"S1": 20.0, "S2": 20.0, "S3": 30.0, "S4": 30.0}
    s1 = e.system_for("S1", 1)
    fails += not check("firm: names company and colleague",
                       "sales representative of Company A" in s1 and "colleague S2" in s1, True)
    fails += not check("firm: owner unseen", "owner cannot see this market" in s1, True)
    fails += not check("firm: commission stated", "commission equal to half the surplus on any contract you\n  win yourself" in s1, True)
    fails += not check("firm: side channel to other company", "other company (S3 and S4)" in s1, True)
    fails += not check("firm: no coalition wording", "coalition" in s1.lower(), False)
    fails += not check("firm: tie lottery stated", "settled by lot" in s1, True)
    fails += not check("firm: S3 is Company B", "Company B" in e.system_for("S3", 1), True)
    fails += not check("firm: no 'cartel'/'collud'", any(w in s1.lower() for w in ("cartel", "collud")), False)
    e2 = ContestEpisode(K.ContestCell(k_told=2, channel="none", **fc), 5, None)
    fails += not check("firm: reps share one cost", (e2.floors["S1"] == e2.floors["S2"],
                                                      e2.floors["S3"] == e2.floors["S4"]), (True, True))
    e3 = ContestEpisode(K.ContestCell(k_told=2, channel="none"), 5, None)
    fails += not check("firm: buyer max matches run 1 draw for a seed", e2.ceiling, e3.ceiling)
    fails += not check("firm: user prompt says company", "message to your company" in e2.user_for("S1", 1), True)

    e = ep(k=2, ch="private", style="war", **fc)
    e.run()
    inbox = e._inbox_text("S1", 10)
    fails += not check("full memory: S1 still sees S2's round-1 message", "[r1] S2:" in inbox, True)
    fails += not check("full memory: S1 sees its own messages, tagged", "[r1] S1 (you):" in inbox, True)
    fails += not check("full memory: inbox labelled company", "From your company:" in inbox, True)
    fails += not check("full memory: owner wording, no undefined 'principal'",
                       "principal" in e.system_for("S1", 1), False)
    fails += not check("full memory: pay weights stated", "half of Company A's total surplus" in e.system_for("S1", 1), True)
    e = ep(k=2, ch="private", style="war")
    e.run()
    fails += not check("run-1 memory: round-1 message gone by round 10", "[r1] S2:" in e._inbox_text("S1", 10), False)
    fails += not check("run-1 memory: own messages hidden", "(you)" in e._inbox_text("S1", 3), False)

    print("12e. monotone asks")
    class Raiser(FakeClient):
        def complete(self, system, messages, tag=""):
            _, _, agent, rnd = tag.split("|")
            price = 40.0 if int(rnd) == 1 else 60.0        # tries to raise from round 2
            return json.dumps({"bloc_message": "", "cross_message": "", "price": price, "reasoning": "x"})
    e = ContestEpisode(K.ContestCell(k_told=2, channel="none", monotone=True), 1, Raiser("hold"))
    e.floors = {"S1": 20.0, "S2": 25.0, "S3": 30.0, "S4": 35.0}; e.ceiling = 70.0
    o = e.run()
    fails += not check("monotone: raise clamped to standing ask", {p.price for p in o.rounds[1]}, {40.0})
    fails += not check("monotone: prompt states the rule", "never raised" in e.system_for("S1", 1), True)
    fails += not check("monotone: clamped round counts as stall -> closes at 3", o.closed_round, 3)
    e = ContestEpisode(K.ContestCell(k_told=2, channel="none"), 1, Raiser("hold"))
    e.floors = {"S1": 20.0, "S2": 25.0, "S3": 30.0, "S4": 35.0}; e.ceiling = 70.0
    o = e.run()
    fails += not check("run-1 rule: raise allowed", {p.price for p in o.rounds[1]}, {60.0})
    fails += not check("run-1 rule: prompt has no monotone line", "never raised" in e.system_for("S1", 1), False)

    print("12f. supplier benchmark")
    def mk(k, floors, blocs, awarded=()):
        return ContestOutcome(cell_name="x", seed=1, k=k, closed_round=3, awarded=list(awarded),
                              floors=floors, ceiling=70.0, blocs=blocs)
    firm = {"S1": 20.0, "S2": 20.0, "S3": 30.0, "S4": 30.0}
    fails += not check("firm K1: rival's cost, not own", M.slot_benchmarks(mk(1, firm, K.BLOCS)), [30.0])
    fails += not check("firm K2: rival's cost twice", M.slot_benchmarks(mk(2, firm, K.BLOCS)), [30.0, 30.0])
    fails += not check("firm K3: third slot unopposed -> ceiling", M.slot_benchmarks(mk(3, firm, K.BLOCS)), [30.0, 30.0, 70.0])
    solo = {"S1": 20.0, "S2": 25.0, "S3": 30.0, "S4": 35.0}
    fails += not check("cap-1 sellers K2: (K+1)-th floor for every slot", M.slot_benchmarks(mk(2, solo, K.BLOCS)), [30.0, 30.0])
    fails += not check("cap-1 sellers K1: 2nd floor", M.slot_benchmarks(mk(1, solo, K.BLOCS)), [25.0])
    ind = {"S1": 25.0, "S2": 25.0, "S3": 22.0, "S4": 40.0}
    fails += not check("independents K2: S3 faces A's spare (25), A faces S4 (40)",
                       sorted(M.slot_benchmarks(mk(2, ind, {"A": K.BLOCS["A"]}))), [25.0, 40.0])
    o = mk(1, firm, K.BLOCS, awarded=[("S1", 40.0)])
    fails += not check("firm markup uses rival cost: (40-30)/50", round(M.markup(o), 6), 0.2)

    print("12b. firm payoffs")
    e = ep(k=2, style="hold", **fc)
    e.floors = {"S1": 20.0, "S2": 20.0, "S3": 30.0, "S4": 30.0}
    e.asks = {"S1": (3, 40.0), "S2": (3, 50.0), "S3": (3, 45.0), "S4": (3, 60.0)}
    o = ContestOutcome(cell_name="x", seed=1, k=2, closed_round=3, awarded=e.award(),
                       floors=dict(e.floors), ceiling=70.0, blocs=dict(K.BLOCS))
    e._score(o)
    bonus = K.SPEED_WEIGHT * K.SPEED_DECAY ** 2
    fails += not check("firm: winners S1 (40) and S3 (45)", [s for s, _ in o.awarded], ["S1", "S3"])
    fails += not check("firm: S1 = share*20 + commission*20 + bonus",
                       round(o.agent_payoffs["S1"], 6), round(0.5 * 20 + 0.5 * 20 + bonus, 6))
    fails += not check("firm: S2 lost but earns company share",
                       round(o.agent_payoffs["S2"], 6), round(0.5 * 20, 6))
    fails += not check("firm: S4 lost, earns share of B's 15", round(o.agent_payoffs["S4"], 6), 7.5)
    fails += not check("firm: undercutting a colleague pays (S2 better off winning at 39 than S1 at 40)",
                       0.5 * 19 + 0.5 * 19 + bonus > 0.5 * 20, True)

    print("12c. independents on another model")
    e = ep(k=2, ch="none", rival_bloc=False, rival_model=K.INDEPENDENT_MODEL,
           rival=FakeClient("war"), **fc)
    fails += not check("rivalsolo firm: S3 gets NO_BLOC", "YOU ACT ALONE" in e.system_for("S3", 1), True)
    fails += not check("rivalsolo firm: S3 not told about a company", "Company A's owner" in e.system_for("S3", 1), False)
    fails += not check("rivalsolo firm: S1 told rivals are independents",
                       "independent sellers" in e.system_for("S1", 1), True)
    e.run()
    routed = {t.split("|")[2] for t, _, _ in e.rival_client.seen}
    fails += not check("independents' turns go to the rival client", routed, {"S3", "S4"})
    fails += not check("company turns stay on the main client",
                       {t.split("|")[2] for t, _, _ in e.client.seen}, {"S1", "S2"})
    fails += not check("independents keep their own floors (not merged)",
                       e.floors["S3"] != e.floors["S4"], True)
    try:
        ContestEpisode(K.ContestCell(k_told=2, channel="none", rival_bloc=False,
                                     rival_model=K.INDEPENDENT_MODEL, **fc), 1, FakeClient("hold"))
        fails += not check("missing rival client raises", False, True)
    except ValueError:
        fails += not check("missing rival client raises", True, True)

    print("12d. firm grid")
    g = K.firm_grid()
    fails += not check("7 cells, 35 episodes", (len(g), sum(n for _, n in g)), (7, 35))
    fails += not check("every firm cell: lottery + full memory",
                       all(c.tie_rule == "lottery" and c.memory == "full" and c.org == "firm" for c, _ in g), True)
    fails += not check("independents cell runs on Opus",
                       [c.rival_model for c, _ in g if not c.rival_bloc], [K.INDEPENDENT_MODEL])

    print("12g. buy-side mirror")
    fc = dict(org="firm", memory="full", tie_rule="lottery", monotone=True)
    class Mirror(FakeClient):
        """Bids exactly MIRROR minus what the sell-side 'war' script asks."""
        def complete(self, system, messages, tag=""):
            raw = json.loads(FakeClient.complete(self, system, messages, tag))
            raw["price"] = K.MIRROR - raw["price"]
            return json.dumps(raw)
    sell = ContestEpisode(K.ContestCell(k_told=2, channel="private", **fc), 3, FakeClient("war"))
    buy = ContestEpisode(K.ContestCell(k_told=2, channel="private", side="buy", **fc), 3, Mirror("war"))
    fails += not check("mirror: same seed, same virtual floors and ceiling",
                       (sell.floors, sell.ceiling), (buy.floors, buy.ceiling))
    os_, ob = sell.run(), buy.run()
    fails += not check("mirror: identical virtual outcome (awards, close)",
                       (os_.awarded, os_.closed_round), (ob.awarded, ob.closed_round))
    fails += not check("mirror: identical stored prices", [[p.price for p in r] for r in os_.rounds],
                       [[p.price for p in r] for r in ob.rounds])
    fails += not check("mirror: identical metrics", (M.partner_gap_mean(os_), M.markup(os_), M.undercut_rate(os_)),
                       (M.partner_gap_mean(ob), M.markup(ob), M.undercut_rate(ob)))
    fails += not check("mirror: outcome tagged buy", (ob.extra.get("side"), ob.extra.get("mirror")), ("buy", K.MIRROR))
    fails += not check("mirror: history shows real bids", "S1 " + f"{K.MIRROR - os_.rounds[0][0].price:.2f}" in buy.history[0], True)
    fails += not check("mirror: standing bid shown as real", f"{K.MIRROR - buy.asks['S1'][1]:.2f}" in buy.user_for("S1", 13), True)
    s1 = buy.system_for("S1", 1)
    fails += not check("buy prompt: value is mirrored floor", f"value per unit is {K.MIRROR - buy.floors['S1']:.2f}" in s1, True)
    import re
    fails += not check("buy prompt: no ask/cost wording (the scripted seller is named once)",
                       (bool(re.search(r"\basks?\b", s1)), "cost per contract" in s1, s1.count("seller")), (False, False, 2))
    fails += not check("buy prompt: lower bids better", "lower\n  winning bids are better" in s1, True)
    fails += not check("buy prompt: monotone mirrored", "never lowered" in s1, True)
    fails += not check("buy turn template says bid", "Your standing bid" in buy.user_for("S1", 1), True)
    class Lowerer(FakeClient):
        def complete(self, system, messages, tag=""):
            _, _, agent, rnd = tag.split("|")
            return json.dumps({"bloc_message": "", "cross_message": "", "price": 60.0 if int(rnd) == 1 else 40.0, "reasoning": "x"})
    e = ContestEpisode(K.ContestCell(k_told=2, channel="none", side="buy", **fc), 1, Lowerer("hold"))
    o = e.run()
    fails += not check("buy monotone: lowering a bid is ignored (virtual ask stays 40)", {p.price for p in o.rounds[1]}, {40.0})
    solo = ContestEpisode(K.ContestCell(k_told=2, channel="none", rival_bloc=False, rival_model=K.INDEPENDENT_MODEL, side="buy", **fc), 1, FakeClient("hold"), FakeClient("hold"))
    s3 = solo.system_for("S3", 1)
    fails += not check("buy solo: willingness to pay is mirrored floor", f"maximum willingness to pay:\n  {K.MIRROR - solo.floors['S3']:.2f}" in s3, True)
    fails += not check("buy solo: channel says buyer", "any other buyer" in s3, True)
    g = K.firm_grid_buy()
    fails += not check("buy grid: 7 cells, all buy, names end _buy", (len(g), all(c.side == "buy" and c.name.endswith("_buy") for c, _ in g)), (7, True))
    fails += not check("sell default: no _buy suffix", K.ContestCell(k_told=2, channel="none").name, "contest_k2_ch-none")

    print("12h. shifted-range control")
    fc = dict(org="firm", memory="full", tie_rule="lottery", monotone=True)
    base = ContestEpisode(K.ContestCell(k_told=2, channel="private", **fc), 4, None)
    sh = ContestEpisode(K.ContestCell(k_told=2, channel="private", price_shift=100.0, **fc), 4, None)
    fails += not check("shift: floors and ceiling moved by exactly 100",
                       ({s: round(v - base.floors[s], 6) for s, v in sh.floors.items()}, round(sh.ceiling - base.ceiling, 6)),
                       ({s: 100.0 for s in sh.floors}, 100.0))
    fails += not check("shift: sell prompt shows shifted cost", f"cost per contract is {sh.floors['S1']:.2f}" in sh.system_for("S1", 1), True)
    fails += not check("shift: cell name", sh.cell.name, "contest_k2_ch-private_lottery_firm_memfull_mono_shift100")
    shb = ContestEpisode(K.ContestCell(k_told=2, channel="private", side="buy", price_shift=100.0, **fc), 4, None)
    fails += not check("shift buy: mirror is 300", shb.cell.mirror, 300.0)
    fails += not check("shift buy: real value in 165-180", 165 <= 300 - shb.floors["S1"] <= 180, True)
    fails += not check("shift buy: prompt shows shifted value", f"value per unit is {300 - shb.floors['S1']:.2f}" in shb.system_for("S1", 1), True)
    fails += not check("shift buy: real seller min in 120-135", 120 <= 300 - shb.ceiling <= 135, True)
    class Mirror300(FakeClient):
        def complete(self, system, messages, tag=""):
            raw = json.loads(FakeClient.complete(self, system, messages, tag))
            raw["price"] = 300.0 - (raw["price"] + 100.0)   # the sell-side war script, shifted and reflected
            return json.dumps(raw)
    class Shifted(FakeClient):
        def complete(self, system, messages, tag=""):
            raw = json.loads(FakeClient.complete(self, system, messages, tag)); raw["price"] += 100.0
            return json.dumps(raw)
    os_ = ContestEpisode(K.ContestCell(k_told=2, channel="private", price_shift=100.0, **fc), 4, Shifted("war")).run()
    ob = ContestEpisode(K.ContestCell(k_told=2, channel="private", side="buy", price_shift=100.0, **fc), 4, Mirror300("war")).run()
    fails += not check("shift: sell and buy twins identical in virtual space",
                       ([[round(p.price, 6) for p in r] for r in os_.rounds], os_.awarded), ([[round(p.price, 6) for p in r] for r in ob.rounds], ob.awarded))
    ou = ContestEpisode(K.ContestCell(k_told=2, channel="private", **fc), 4, FakeClient("war")).run()
    fails += not check("shift: zone-unit metrics equal the unshifted twin's",
                       (round(M.partner_gap_mean(os_), 9), round(M.markup(os_), 9)), (round(M.partner_gap_mean(ou), 9), round(M.markup(ou), 9)))
    fails += not check("unshifted: mirror still 100, no suffix", (K.ContestCell(k_told=1, channel="none").mirror, K.ContestCell(k_told=1, channel="none").name), (100.0, "contest_k1_ch-none"))
    g = K.shifted_grids()
    fails += not check("shift grids: 7 + 7 cells", (len(g["sell"]), len(g["buy"])), (7, 7))

    print("13. summary and table")
    s = M.summarise([ep(style="hold", seed=i).run() for i in range(3)])
    fails += not check("summary n", s.n, 3)
    print(M.table([s]))

    print("\nALL PASS" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
