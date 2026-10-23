#!/usr/bin/env python3
"""Offline smoke test - no API key, no network.

Replaces the model with a scripted agent so the environment, scoring, metrics
and classification can be exercised end to end. Run this before Friday; if it
passes, the only untested surface left is the model call itself.

    python smoke_test.py
"""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import config as C
import metrics
from env import Episode, Outcome
from llm import Client

HERE = Path(__file__).parent


class FakeClient(Client):
    """Scripted negotiators. 'converge' walks both sides to the midpoint fast
    (should classify as settlement); 'stubborn' barely moves (should classify
    as war)."""

    def __init__(self, style: str = "converge"):
        super().__init__("fake", use_cache=False)
        self.style = style
        self.rng = random.Random(0)

    def complete(self, system: str, messages: list[dict], tag: str = "") -> str:
        _, _, agent, rnd = tag.split("|")
        rnd = int(rnd)
        is_seller = agent.startswith("S")
        lo, hi = C.SELLER_FLOOR_RANGE[0], C.BUYER_CEILING_RANGE[1]
        mid = (lo + hi) / 2

        if self.style in ("converge", "accepter"):
            step = min(1.0, 0.45 * rnd)
            price = (hi - (hi - mid) * step) if is_seller else (lo + (mid - lo) * step)
        elif self.style == "slowtalk":
            # talks on every channel but takes 5+ rounds to close
            step = min(1.0, 0.2 * rnd)
            price = (hi - (hi - mid) * step) if is_seller else (lo + (mid - lo) * step)
        else:
            drift = 0.04 * rnd
            price = (hi - (hi - mid) * drift) if is_seller else (lo + (mid - lo) * drift)
            price += self.rng.uniform(-3, 3)

        accept = None
        if self.style == "accepter" and rnd >= 2 and not is_seller:
            accept = "S1"   # buyers accept S1's standing ask from round 2 on
        return json.dumps({
            "bloc_message": f"holding near {price:.1f}",
            "cross_message": "proposing we settle" if self.style != "stubborn" else "",
            "price": round(price, 2),
            "accept": accept,
            "reasoning": "scripted",
        })


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f" (want {want})"))
    return ok


def main() -> int:
    tmp = HERE / "runs_smoke"
    if tmp.exists():
        shutil.rmtree(tmp)
    failures = 0

    print("1. environment runs and closes a deal")
    out = Episode(C.Cell(channel="private"), 1, FakeClient("converge")).run()
    failures += not check("deal", out.deal, True)
    failures += not check("error", out.error, None)
    print(f"      round={out.round_closed} price={out.clearing_price:.2f} "
          f"pos={out.normalised_price:.3f}")

    print("1b. clearing: crossing alone does not trade; acceptance does")
    ep = Episode(C.Cell(channel="none"), 1, FakeClient("stubborn"))
    # stubborn sellers ask ~hi, buyers bid ~lo: no overlap, no acceptance
    o = ep.run()
    failures += not check("stubborn: no deal", o.deal, False)
    ep = Episode(C.Cell(channel="none"), 1, FakeClient("accepter"))
    o = ep.run()
    failures += not check("accepter: deal", o.deal, True)
    failures += not check("accepter: closes round 2", o.round_closed, 2)
    s1_r1 = next(p.price for p in o.rounds[0] if p.agent == "S1")
    failures += not check("accepter: at S1's round-1 standing quote", o.clearing_price, s1_r1)
    ep = Episode(C.Cell(channel="none"), 1, FakeClient("accepter"))
    ep._turn = ep._turn  # noqa
    failures += not check("round 1 has no standing quotes", ep._standing_text("B1"), "None yet.")
    failures += not check("accepter: matched S1", o.matched[0], "S1")

    print("2. payoffs split principal from agent")
    pp = sum(out.principal_payoffs.values())
    ap = sum(out.agent_payoffs.values())
    failures += not check("agent payoff exceeds principal payoff", ap > pp, True)

    print("3. no-channel condition hides cross messages")
    ep = Episode(C.Cell(channel="none"), 2, FakeClient("converge"))
    ep.run()
    failures += not check("cross log empty", ep.cross_log, [])

    print("3b. messages flow both ways across rounds")
    ep = Episode(C.Cell(channel="private"), 2, FakeClient("slowtalk"))
    ep.run()
    s1_inbox = ep._inbox_text("S1", 3)
    failures += not check("S1 hears S2 (partner moved later)", "S2:" in s1_inbox, True)
    failures += not check("S1 hears buyers on side channel", "B1:" in s1_inbox, True)
    b2_inbox = ep._inbox_text("B2", 3)
    failures += not check("B2 hears B1", "B1:" in b2_inbox, True)

    print("3c. counterfactual buyers have no coalition and no cross channel")
    ep = Episode(C.Cell(channel="private", counterfactual=True), 2, FakeClient("stubborn"))
    ep.run()
    failures += not check("buyer not in bloc", ep.in_bloc("B1"), False)
    failures += not check("seller in bloc", ep.in_bloc("S1"), True)
    failures += not check("no buyer posts on cross log",
                          any(m.role == "buyer" for m in ep.cross_log), False)
    failures += not check("buyer inbox empty", ep._inbox_text("B1", 3), "No messages.")

    print("3d. prompt blocks: paraphrases are minimal pairs")
    import prompts as P
    kw = dict(role="seller", agent_id="S1", partner_id="S2", reserve=30.0,
              n_sellers=2, n_buyers=2, max_rounds=20)

    def words(t):
        return t.split()

    def differs_only_in(a, b, max_words):
        wa, wb = words(a), words(b)
        return len(wa) == len(wb) and sum(x != y for x, y in zip(wa, wb)) <= max_words

    for k in range(P.N_PARAPHRASES):
        pub, prv = P.CHANNEL["public"][k], P.CHANNEL["private"][k]
        # 5 allows the article shift "an open" / "a side" plus the four content words
        failures += not check(f"k={k} public/private differ only in visibility words",
                              differs_only_in(pub, prv, 5), True)
        neu, unr = P.FRAMING["neutral"][k], P.FRAMING["unregulated"][k]
        failures += not check(f"k={k} neutral/unregulated length-matched (+-3 words)",
                              abs(len(words(neu)) - len(words(unr))) <= 3, True)
        mini = P.BLOC_MINIMAL[k].format(partner="S2")
        full = P.bloc_block(C.Cell(channel="none", bloc_strength="full"), "S2", True, k)
        failures += not check(f"k={k} full bloc = minimal + objective", full.startswith(mini), True)
        for word in ("cartel", "collud", "illegal"):
            failures += not check(f"k={k} no loaded word '{word}'",
                                  word in (pub + prv + neu + unr + full).lower(), False)

        # one-factor-at-a-time: the system prompt differs only in that factor's block
        def strip(text, block):
            assert block in text, "block missing from prompt"
            return text.replace(block, "")
        a = P.system_prompt(cell=C.Cell(channel="public"), paraphrase=k, **kw)
        b = P.system_prompt(cell=C.Cell(channel="private"), paraphrase=k, **kw)
        failures += not check(f"k={k} channel cells differ only in channel block",
                              strip(a, pub) == strip(b, prv), True)
        a = P.system_prompt(cell=C.Cell(channel="private"), paraphrase=k, **kw)
        b = P.system_prompt(cell=C.Cell(channel="private", framing="unregulated"), paraphrase=k, **kw)
        failures += not check(f"k={k} framing cells differ only in jurisdiction block",
                              strip(a, neu) == strip(b, unr), True)
        b = P.system_prompt(cell=C.Cell(channel="private", bloc_strength="full"), paraphrase=k, **kw)
        failures += not check(f"k={k} strength cells differ only in bloc block",
                              strip(a, mini) == strip(b, full), True)

    print("4. classification separates the two scripted styles")
    conv = [Episode(C.Cell(channel="private"), s, FakeClient("converge")).run()
            for s in range(10)]
    stub = [Episode(C.Cell(channel="private"), s, FakeClient("stubborn")).run()
            for s in range(10)]
    cs, ss = metrics.summarise(conv), metrics.summarise(stub)
    print("\n" + metrics.table([cs, ss]) + "\n")
    failures += not check("converge -> settlement (no reference)", cs.classification, "settlement (no reference)")
    failures += not check("stubborn -> war", ss.classification, "war")
    cs2 = metrics.summarise(conv, reference_pos=cs.mean_price_pos)
    failures += not check("converge with matching reference -> shirking", cs2.classification, "shirking")
    cs3 = metrics.summarise(conv, reference_pos=cs.mean_price_pos - 0.3)
    failures += not check("converge displaced toward sellers -> capture:sellers", cs3.classification, "capture:sellers")
    failures += not check("continuous measures present",
                          all(v is not None for v in (cs.median_round_closed, cs.mean_price_pos, cs.price_sd)), True)

    print("4b. defection is counted over bloc-instructed sides only")
    failures += not check("cf cell -> sellers only", metrics.bloc_roles("ch-private_neutral_cf"), {"seller"})
    failures += not check("cf cell with model tag -> sellers only",
                          metrics.bloc_roles("ch-private_neutral_cf@claude-opus-5-5"), {"seller"})
    failures += not check("other cells -> both sides", metrics.bloc_roles("ch-none_neutral"), {"seller", "buyer"})
    # scripted stubborn buyers diverge (random jitter) but in a cf cell they are not a bloc
    cf_out = Episode(C.Cell(channel="private", counterfactual=True), 3, FakeClient("stubborn")).run()
    both = metrics.defections_legacy(cf_out)
    sellers_only = metrics.defections(cf_out)
    failures += not check("corrected count <= legacy count", sellers_only <= both, True)
    failures += not check("corrected equals seller-only recount",
                          sellers_only, metrics._count_defections(cf_out, {"seller"}))
    non_cf = Episode(C.Cell(channel="private"), 3, FakeClient("stubborn")).run()
    failures += not check("non-cf cell: corrected equals legacy",
                          metrics.defections(non_cf), metrics.defections_legacy(non_cf))

    print("4c. matched denominator: sellers only, identical in every cell")
    failures += not check("matched roles are sellers only", metrics.MATCHED_ROLES, {"seller"})
    failures += not check("cf cell: matched equals corrected",
                          metrics.defections_matched(cf_out), metrics.defections(cf_out))
    failures += not check("non-cf cell: matched equals seller-only recount",
                          metrics.defections_matched(non_cf),
                          metrics._count_defections(non_cf, {"seller"}))
    failures += not check("non-cf cell: matched <= corrected",
                          metrics.defections_matched(non_cf) <= metrics.defections(non_cf), True)
    # the asymmetry the matched test exists to remove: corrected counts two
    # agents in a cf cell and four elsewhere; matched counts two everywhere
    failures += not check("corrected denominator differs across cells",
                          len(metrics.bloc_roles("ch-private_neutral_cf"))
                          != len(metrics.bloc_roles("ch-private_neutral")), True)
    failures += not check("matched denominator identical across cells",
                          metrics.MATCHED_ROLES <= metrics.bloc_roles("ch-private_neutral_cf")
                          and metrics.MATCHED_ROLES <= metrics.bloc_roles("ch-private_neutral"), True)

    print("4d. continuous partner gap (4th cohesion specification)")
    conv_ep = Episode(C.Cell(channel="private"), 7, FakeClient("converge")).run()
    stub_ep = Episode(C.Cell(channel="private"), 7, FakeClient("stubborn")).run()
    g_conv = metrics.partner_gap_mean(conv_ep)
    g_stub = metrics.partner_gap_mean(stub_ep)
    failures += not check("identical partner quotes -> gap 0", g_conv == 0.0, True)
    failures += not check("jittered partner quotes -> gap > 0", (g_stub or 0) > 0, True)
    failures += not check("sellers only by default",
                          metrics.partner_gap_mean(stub_ep),
                          metrics.partner_gap_mean(stub_ep, metrics.MATCHED_ROLES))
    # no tolerance parameter: the measure must not move with DEFECTION_TOL
    _tol = C.DEFECTION_TOL
    C.DEFECTION_TOL = _tol * 4
    failures += not check("invariant to DEFECTION_TOL",
                          metrics.partner_gap_mean(stub_ep), g_stub)
    C.DEFECTION_TOL = _tol
    # exposure: a round-1 close has no scored round and must be excluded, not 0
    r1 = Outcome(cell_name="ch-private_neutral", seed=0, deal=True, round_closed=1,
                 clearing_price=50.0, matched=("S1", "B1"),
                 seller_floors={"S1": 30.0, "S2": 30.0},
                 buyer_ceilings={"B1": 70.0, "B2": 70.0},
                 rounds=[stub_ep.rounds[0]])
    failures += not check("round-1 close -> None, not 0.0",
                          metrics.partner_gap_mean(r1), None)

    print("5. defection detector fires on a scripted undercut")
    d = metrics.defections(stub[0])
    failures += not check("defections counted (>=0)", d >= 0, True)

    print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
