#!/usr/bin/env python3
"""Offline smoke test for the AgenticPay arm. Scripted agents emit AgenticPay's
structured format plus our coalition/cross blocks, so the whole driver, the
tag harvest, the channel gating and the scoring run without an API key.

    external/.venv-ap/bin/python smoke_test_ap.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "external" / "AgenticPay"))
sys.path.insert(0, str(HERE))

import config as C  # noqa: E402
import metrics  # noqa: E402
from llm import Client  # noqa: E402
from run_ap import APEpisode  # noqa: E402


class FakeClient(Client):
    """Walks both sides to the midpoint; closes around round 3."""

    def __init__(self, style: str = "converge"):
        super().__init__("fake", use_cache=False)
        self.style = style

    def complete(self, system: str, messages: list[dict], tag: str = "") -> str:
        _, _, _, pair, rnd = tag.split("|")
        agent, counterpart = pair.split("->")
        rnd = int(rnd)
        is_seller = agent.startswith("S")
        lo, hi = C.SELLER_FLOOR_RANGE[0], C.BUYER_CEILING_RANGE[1]
        mid = (lo + hi) / 2
        step = min(1.0, 0.45 * rnd) if self.style == "converge" else 0.04 * rnd
        price = (hi - (hi - mid) * step) if is_seller else (lo + (mid - lo) * step)
        tag_name = "SELLER_PRICE" if is_seller else "BUYER_PRICE"
        sel = "<selected_buyer>1</selected_buyer>" if is_seller else "<selected_seller>1</selected_seller>"
        return (
            "<mental_model>\n[Opponent Reservation Price]: price=50; confidence=50\n"
            "[Opponent Strategy]: scripted\n[My Strategy]: scripted\n</mental_model>\n"
            f"{sel}\n<message>Offer ### {tag_name}(${price:.2f}) ###</message>\n"
            f"<coalition_message>{agent} holding near {price:.1f}</coalition_message>\n"
            f"<cross_message>{agent} proposes we settle</cross_message>"
        )


def check(label, got, want) -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f" (want {want})"))
    return ok


def main() -> int:
    f = 0
    print("1. private channel: episode runs, deal closes, both logs populated")
    ep = APEpisode(C.Cell(channel="private"), 1, FakeClient())
    out = ep.run()
    f += not check("error", out.error, None)
    f += not check("deal", out.deal, True)
    print(f"      round={out.round_closed} price={out.clearing_price} pos={out.normalised_price:.3f} "
          f"matched={out.matched}")
    f += not check("coalition messages harvested", len(ep.bloc_log["seller"]) > 0, True)
    f += not check("cross messages harvested", len(ep.cross_log) > 0, True)
    f += not check("one coalition post per agent per round",
                   all(sum(1 for m in ep.bloc_log["seller"] if m.sender == "S1" and m.rnd == r) <= 1
                       for r in range(1, 5)), True)
    f += not check("no tags leaked into counterpart thread",
                   any(re.search(r"<(coalition|cross)_message>", m["content"])
                       for m in ep.env.memory_b1s1.get_history()), False)
    f += not check("agent payoff > principal payoff",
                   sum(out.agent_payoffs.values()) > sum(out.principal_payoffs.values()), True)
    f += not check("native scores recorded", "global_score" in out.extra["native"], True)

    print("2. no channel: cross log stays empty, coalition still talks")
    ep = APEpisode(C.Cell(channel="none"), 2, FakeClient())
    ep.run()
    f += not check("cross log empty", ep.cross_log, [])
    f += not check("bloc log populated", len(ep.bloc_log["buyer"]) > 0, True)
    from prompts import CHANNEL, paraphrase_of
    f += not check("suffix carries the no-channel block for this seed's paraphrase",
                   CHANNEL["none"][paraphrase_of(2)] in ep._suffix("S1", 2), True)

    print("3. counterfactual: buyers alone, sellers keep bloc and channel")
    ep = APEpisode(C.Cell(channel="private", counterfactual=True), 3, FakeClient())
    ep.run()
    f += not check("buyer cross posts", any(m.role == "buyer" for m in ep.cross_log), False)
    f += not check("buyer bloc posts", ep.bloc_log["buyer"], [])
    f += not check("seller cross posts", any(m.role == "seller" for m in ep.cross_log), True)
    f += not check("buyer suffix has no extra blocks", "coalition_message" in ep._suffix("B1", 2), False)

    print("4. seeds pair with the self-contained arm")
    from env import Episode
    from smoke_test import FakeClient as SelfFake
    a = Episode(C.Cell(channel="private"), 7, SelfFake("converge"))
    b = APEpisode(C.Cell(channel="private"), 7, FakeClient())
    f += not check("same floors", a.floors, b.floors)
    f += not check("same ceilings", a.ceilings, b.ceilings)

    print("5. classification works on this arm's outcomes")
    conv = [APEpisode(C.Cell(channel="private"), s, FakeClient()).run() for s in range(4)]
    stub = [APEpisode(C.Cell(channel="private"), s, FakeClient("stubborn")).run() for s in range(4)]
    cs, ss = metrics.summarise(conv), metrics.summarise(stub)
    print("\n" + metrics.table([cs, ss]) + "\n")
    f += not check("converge -> settlement (no reference)", cs.classification, "settlement (no reference)")
    f += not check("stubborn -> war", ss.classification, "war")
    print(f"      stubborn rounds played: {len(stub[0].rounds)} (want {C.MAX_ROUNDS})")
    f += not check("round cap honoured", len(stub[0].rounds), C.MAX_ROUNDS)

    print(f"\n{'ALL PASS' if not f else str(f) + ' FAILURE(S)'}")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
