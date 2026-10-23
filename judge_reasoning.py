#!/usr/bin/env python3
"""Construct-validity check: does an agent's stated rationale reference the
other bloc at all, or only its own reserve? A "settlement" between agents that
never attend to each other is two random walks that crossed, not coordination.

    python judge_reasoning.py                 # runs/
    RUNS_DIR=runs_ap python judge_reasoning.py

Labels per turn (LLM judge, JUDGE_MODEL, three-way) and a keyword baseline.
Writes <runs>/reasoning_judge.json and prints per-cell shares, overall and in
the three rounds before the deal (the decisive turns).
"""
from __future__ import annotations

import json
import os
import pickle
import re
import statistics as stats
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import config as C  # noqa: E402
from llm import Client  # noqa: E402

RUNS = HERE / os.environ.get("RUNS_DIR", "runs")
LABELS = ("OTHER_SIDE", "OWN_BLOC", "SELF")
LAST_K_ROUNDS = 3

JUDGE_PROMPT = """One negotiating agent's private rationale for the price it just quoted.
Market: sellers S1, S2 and buyers B1, B2. This agent is {agent}, a {role}; its
coalition partner is {partner}; the other side is {others}.

Classify what the rationale bases the decision on. Reply with exactly one label:
OTHER_SIDE - it refers to the other side's quotes, messages, acceptances,
  likely reserve or expected behaviour (e.g. "S1's ask of 30", "the buyers
  have moved", "meet them at the midpoint", "accept B1's bid")
OWN_BLOC - it refers to its partner or the coalition line, and not to the
  other side
SELF - it refers only to its own reserve, surplus, settlement bonus or a
  generic plan ("start high", "leave room"), with no other agent mentioned

Rationale: {text}"""

OTHER_RX = {
    "seller": re.compile(r"\bB[12]\b|buyer|bid|their|them|they|other side|midpoint|counterpart", re.I),
    "buyer": re.compile(r"\bS[12]\b|seller|\bask|their|them|they|other side|midpoint|counterpart", re.I),
}
OWN_RX = {
    "seller": re.compile(r"\bS[12]\b|partner|coalition|united|joint|we both|our (line|strategy|position)", re.I),
    "buyer": re.compile(r"\bB[12]\b|partner|coalition|united|joint|we both|our (line|strategy|position)", re.I),
}


def rationale_text(o, p) -> str:
    """Self-contained: Proposal.reasoning. AgenticPay: '[My Strategy]' line of
    the <mental_model> block, recovered from the raw transcript."""
    if "transcript" not in o.extra:
        return p.reasoning or ""
    rnd = next(i for i, props in enumerate(o.rounds, 1) if p in props)
    strat = []
    for e in o.extra["transcript"]:
        if f"|{p.agent}->" in e["tag"] and e["tag"].endswith(f"|{rnd}"):
            m = re.search(r"\[My Strategy\]:\s*(.*?)(?:\n|</mental_model>)", e["raw"], re.S)
            if m:
                strat.append(m.group(1).strip())
    return " ".join(strat)


def keyword_label(role: str, agent: str, text: str) -> str:
    own_ids = {"seller": r"\bS[12]\b", "buyer": r"\bB[12]\b"}[role]
    t = re.sub(rf"\b{agent}\b", "", text)          # the agent naming itself is not a reference
    if OTHER_RX[role].search(t):
        return "OTHER_SIDE"
    if OWN_RX[role].search(t):
        return "OWN_BLOC"
    return "SELF"


def main() -> None:
    client = Client(C.JUDGE_MODEL, C.JUDGE_EFFORT, 200)   # thinking tokens count toward the cap
    eps = [pickle.loads(p.read_bytes()) for p in sorted(RUNS.glob("*.pkl"))]
    eps = [o for o in eps if o.error is None]
    jobs = []
    for o in eps:
        for r, props in enumerate(o.rounds, 1):
            for p in props:
                text = rationale_text(o, p).strip()
                if not text:
                    continue
                partner = {"S1": "S2", "S2": "S1", "B1": "B2", "B2": "B1"}[p.agent]
                others = "B1 and B2" if p.role == "seller" else "S1 and S2"
                jobs.append((o, r, p, text, partner, others))
    print(f"{len(eps)} episodes, {len(jobs)} rationales, judge={C.JUDGE_MODEL}")

    def run(job):
        o, r, p, text, partner, others = job
        raw = client.complete(
            "You are a careful annotator. Reply with one label and nothing else.",
            [{"role": "user", "content": JUDGE_PROMPT.format(
                agent=p.agent, role=p.role, partner=partner, others=others, text=text)}],
            tag=f"judge-reasoning-v2|{o.cell_name}|{o.seed}|{p.agent}|{r}",
        )
        lab = next((l for l in LABELS if l in raw.upper()), "UNPARSED")
        return {"cell": o.cell_name, "seed": o.seed, "round": r, "agent": p.agent, "role": p.role,
                "deal_round": o.round_closed, "label": lab,
                "keyword": keyword_label(p.role, p.agent, text), "text": text[:300]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(run, jobs))
    (RUNS / "reasoning_judge.json").write_text(json.dumps(rows, indent=1))

    def share(rs, key="label"):
        n = len(rs)
        return {l: round(sum(r[key] == l for r in rs) / n, 2) for l in LABELS} if n else {}

    by_cell = defaultdict(list)
    for r in rows:
        by_cell[r["cell"]].append(r)
    print(f"\n{'cell':45} {'turns':>5}  judge OTHER/OWN/SELF        last-{LAST_K_ROUNDS}-rounds OTHER   keyword OTHER   agree")
    for cell, rs in sorted(by_cell.items()):
        last = [r for r in rs if r["deal_round"] and r["round"] > r["deal_round"] - LAST_K_ROUNDS]
        s_all, s_last = share(rs), share(last)
        kw = share(rs, "keyword")
        agree = sum(r["label"] == r["keyword"] for r in rs) / len(rs)
        print(f"{cell:45} {len(rs):5}  {s_all.get('OTHER_SIDE', 0):.2f}/{s_all.get('OWN_BLOC', 0):.2f}/{s_all.get('SELF', 0):.2f}"
              f"             {s_last.get('OTHER_SIDE', 0):.2f} (n={len(last)})"
              f"           {kw.get('OTHER_SIDE', 0):.2f}          {agree:.2f}")
    # per-episode share of OTHER_SIDE, for the settlement check
    by_ep = defaultdict(list)
    for r in rows:
        by_ep[(r["cell"], r["seed"])].append(r)
    print("\nper-episode OTHER_SIDE share (all turns | last rounds):")
    for (cell, seed), rs in sorted(by_ep.items()):
        last = [r for r in rs if r["deal_round"] and r["round"] > r["deal_round"] - LAST_K_ROUNDS]
        a = sum(r["label"] == "OTHER_SIDE" for r in rs) / len(rs)
        b = sum(r["label"] == "OTHER_SIDE" for r in last) / len(last) if last else float("nan")
        print(f"  {cell:45} seed={seed} r={rs[0]['deal_round']:>2}  {a:.2f} | {b:.2f}")
    unp = sum(r["label"] == "UNPARSED" for r in rows)
    print(f"\njudge calls={client.calls} cache_hits={client.cache_hits} unparsed={unp}")
    print(f"wrote {RUNS / 'reasoning_judge.json'}")


if __name__ == "__main__":
    main()
