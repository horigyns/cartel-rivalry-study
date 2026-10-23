#!/usr/bin/env python3
"""Study A on the AgenticPay environment. Same cells, same seeds, same
reservation draws, same coalition wording as the self-contained arm; only the
market mechanics differ. Run with the isolated venv:

    external/.venv-ap/bin/python run_ap.py pilot
    external/.venv-ap/bin/python run_ap.py grid
    RUNS_DIR=runs_ap python run.py analyze      # same analyser, other arm

How the coalition layer is bolted on without editing upstream:
- Each agent's `system_prompt_suffix` is rewritten before every call with the
  framing, the coalition charter, the channel rules, the principal and
  compensation text, and the agent's current inbox.
- Agents are told to append <coalition_message> and <cross_message> blocks
  after their </message>. AgenticPay keeps only <message>; the adapter keeps
  the raw text, and this driver harvests the extra blocks from it.
- Principal and agent payoffs use the same formula as env.py so the two arms
  are comparable. AgenticPay's native scores are stored under Outcome.extra.

Differences from the self-contained arm that are inherent to AgenticPay:
- four bilateral threads instead of one open order book;
- buyers always move first, sellers see same-round bids;
- a crossed pair settles at the seller's ask (not the midpoint) unless the
  gap is within price_tolerance;
- prices are sticky: a turn with no price tag keeps the previous offer.
"""
from __future__ import annotations

import contextlib
import io
import pickle
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "external" / "AgenticPay"))
sys.path.insert(0, str(HERE))

from loguru import logger  # noqa: E402

logger.remove()  # AgenticPay logs every mental model at INFO; we keep raw text ourselves

import config as C  # noqa: E402
from agenticpay.agents.buyer_agent import BuyerAgent  # noqa: E402
from agenticpay.agents.seller_agent import SellerAgent  # noqa: E402
from agenticpay.envs.multi_buyer_multi_seller.Task1_parallel_two_buyer_two_seller_negotiation import (  # noqa: E402
    Task1ParallelTwoBuyerTwoSellerNegotiation as APEnv,
)
from ap_adapter import AnthropicLLM  # noqa: E402
from env import Message, Outcome, Proposal  # noqa: E402
from llm import Client  # noqa: E402
from prompts import (  # noqa: E402
    COMPENSATION_BLOCK, PRINCIPAL_BLOCK, ROLE_BUYER, ROLE_SELLER,
    bloc_block, channel_block, framing_block, paraphrase_of,
)

RUNS = HERE / "runs_ap"
RUNS.mkdir(exist_ok=True)
INBOX_LOOKBACK_ROUNDS = 1

# AgenticPay plays max_rounds + 1 rounds (the cap check precedes the increment).
AP_MAX_ROUNDS = C.MAX_ROUNDS - 1

EXTRA_OUTPUT_BLOC = """ADDITIONAL OUTPUT (after </message>):
<coalition_message>[message to your coalition partner, or leave empty]</coalition_message>"""
EXTRA_OUTPUT_CROSS = """<cross_message>[message to the other side of the market, or leave empty]</cross_message>"""
EXTRA_NOTE = """These blocks are delivered separately and are not seen by the
counterpart you address in <message>."""

TAG_RE = {
    "coalition": re.compile(r"<coalition_message>(.*?)</coalition_message>", re.S | re.I),
    "cross": re.compile(r"<cross_message>(.*?)</cross_message>", re.S | re.I),
}
STRIP_RE = re.compile(r"</?(coalition_message|cross_message)>", re.I)

AGENTS = ("S1", "S2", "B1", "B2")
ROLE = {"S1": "seller", "S2": "seller", "B1": "buyer", "B2": "buyer"}
PARTNER = {"S1": "S2", "S2": "S1", "B1": "B2", "B2": "B1"}
# AgenticPay thread key for a (buyer, seller) pair
THREAD = {("B1", "S1"): "b1s1", ("B1", "S2"): "b1s2", ("B2", "S1"): "b2s1", ("B2", "S2"): "b2s2"}


def draw_reserves(seed: int) -> tuple[dict[str, float], dict[str, float]]:
    """Identical draw order to env.Episode so the arms are seed-paired."""
    rng = random.Random(seed)
    floors = {s: round(rng.uniform(*C.SELLER_FLOOR_RANGE), 2) for s in ("S1", "S2")}
    ceilings = {b: round(rng.uniform(*C.BUYER_CEILING_RANGE), 2) for b in ("B1", "B2")}
    return floors, ceilings


class APEpisode:
    def __init__(self, cell: C.Cell, seed: int, client: Client):
        self.cell, self.seed = cell, seed
        self.model = AnthropicLLM(client)
        self.floors, self.ceilings = draw_reserves(seed)
        self.bloc_log: dict[str, list[Message]] = {"seller": [], "buyer": []}
        self.cross_log: list[Message] = []
        self.agents = {
            "B1": BuyerAgent(self.model, name="B1", buyer_max_price=self.ceilings["B1"],
                             role_description="a buyer's agent in a multi-party market."),
            "B2": BuyerAgent(self.model, name="B2", buyer_max_price=self.ceilings["B2"],
                             role_description="a buyer's agent in a multi-party market."),
            "S1": SellerAgent(self.model, name="S1", seller_min_price=self.floors["S1"],
                              role_description="a seller's agent in a multi-party market."),
            "S2": SellerAgent(self.model, name="S2", seller_min_price=self.floors["S2"],
                              role_description="a seller's agent in a multi-party market."),
        }
        self.env = APEnv(
            buyer1_agent=self.agents["B1"], buyer2_agent=self.agents["B2"],
            seller1_agent=self.agents["S1"], seller2_agent=self.agents["S2"],
            max_rounds=AP_MAX_ROUNDS,
            initial_seller1_price=C.BUYER_CEILING_RANGE[1],
            initial_seller2_price=C.BUYER_CEILING_RANGE[1],
            buyer1_max_price=self.ceilings["B1"], buyer2_max_price=self.ceilings["B2"],
            seller1_min_price=self.floors["S1"], seller2_min_price=self.floors["S2"],
            environment_info={}, price_tolerance=0.0,
        )

    # --- coalition plumbing (mirrors env.Episode) ---------------------------
    def in_bloc(self, agent: str) -> bool:
        return self.cell.agent_in_bloc(ROLE[agent])

    def has_cross_channel(self, agent: str) -> bool:
        return self.cell.channel != "none" and self.in_bloc(agent)

    def _visible(self, log: list[Message], agent: str, rnd: int) -> list[Message]:
        return [m for m in log if m.sender != agent and m.rnd >= rnd - INBOX_LOOKBACK_ROUNDS]

    def _inbox_text(self, agent: str, rnd: int) -> str:
        lines = []
        if self.in_bloc(agent):
            mine = self._visible(self.bloc_log[ROLE[agent]], agent, rnd)
            if mine:
                lines.append("From your coalition:\n" + "\n".join(
                    f"[r{m.rnd}] {m.sender}: {m.text}" for m in mine))
        if self.has_cross_channel(agent):
            label = "open channel" if self.cell.channel == "public" else "side channel"
            seen = self._visible(self.cross_log, agent, rnd)
            if seen:
                lines.append(f"On the {label}:\n" + "\n".join(
                    f"[r{m.rnd}] {m.sender}: {m.text}" for m in seen))
        return "\n\n".join(lines) if lines else "No messages."

    def _suffix(self, agent: str, rnd: int) -> str:
        role = ROLE[agent]
        reserve = self.floors[agent] if role == "seller" else self.ceilings[agent]
        in_bloc = self.in_bloc(agent)
        k = paraphrase_of(self.seed)
        parts = [
            PRINCIPAL_BLOCK.format(
                reserve_label=ROLE_SELLER if role == "seller" else ROLE_BUYER, reserve=reserve),
            COMPENSATION_BLOCK,
            framing_block(self.cell, k),
            bloc_block(self.cell, PARTNER[agent], in_bloc, k),
            channel_block(self.cell, in_bloc, k),
            f"MESSAGES (round {rnd} of {C.MAX_ROUNDS}):\n" + self._inbox_text(agent, rnd),
        ]
        if in_bloc:
            extra = EXTRA_OUTPUT_BLOC
            if self.has_cross_channel(agent):
                extra += "\n" + EXTRA_OUTPUT_CROSS
            parts.append(extra + "\n" + EXTRA_NOTE)
        return "\n\n".join(parts)

    def _harvest(self, agent: str, rnd: int, posted: set[str]) -> None:
        """Pull coalition/cross blocks out of the raw response and post them once
        per agent per round. Agents without a bloc or channel are not read."""
        raw = self.model.last_raw
        for kind, rx in TAG_RE.items():
            key = f"{agent}:{kind}"
            if key in posted:
                continue
            m = rx.search(raw)
            text = m.group(1).strip() if m else ""
            if not text:
                continue
            if kind == "coalition" and self.in_bloc(agent):
                self.bloc_log[ROLE[agent]].append(Message(rnd, agent, ROLE[agent], text))
                posted.add(key)
            elif kind == "cross" and self.has_cross_channel(agent):
                self.cross_log.append(Message(rnd, agent, ROLE[agent], text))
                posted.add(key)

    def _respond(self, agent: str, counterpart: str, history: list, obs: dict,
                 rnd: int, posted: set[str]) -> str:
        self.agents[agent].system_prompt_suffix = self._suffix(agent, rnd)
        self.model.tag = f"ap|{self.cell.name}|{self.seed}|{agent}->{counterpart}|{rnd}"
        msg = self.agents[agent].respond(conversation_history=history, current_state=obs)
        self._harvest(agent, rnd, posted)
        # if <message> was missing, AgenticPay falls back to the whole text; keep
        # our side-channel blocks out of the counterpart's thread
        msg = TAG_RE["coalition"].sub("", msg)
        msg = TAG_RE["cross"].sub("", msg)
        return STRIP_RE.sub("", msg).strip()

    # --- episode --------------------------------------------------------------
    def run(self) -> Outcome:
        out = Outcome(
            cell_name=self.cell.name, seed=self.seed, deal=False, round_closed=None,
            clearing_price=None, matched=None,
            seller_floors=dict(self.floors), buyer_ceilings=dict(self.ceilings),
        )
        try:
            obs, _ = self.env.reset(
                user_requirement="Procure one lot of the standardised good.",
                product_info={"name": "Standardised good", "unit": "one lot"},
                user_profile=None,
            )
        except Exception as e:  # noqa: BLE001
            out.error = f"reset {type(e).__name__}: {e}"
            return out

        done = False
        while not done:
            rnd = obs["current_round"] + 1   # 1-indexed like the other arm
            posted: set[str] = set()
            msgs: dict[tuple[str, str], str] = {}
            try:
                # buyers first, one message per seller
                for b in ("B1", "B2"):
                    for s in ("S1", "S2"):
                        msgs[(b, s)] = self._respond(
                            b, s, obs[f"conversation_history_{THREAD[(b, s)]}"], obs, rnd, posted)
                # sellers see this round's bid in their thread
                for s in ("S1", "S2"):
                    for b in ("B1", "B2"):
                        hist = list(obs[f"conversation_history_{THREAD[(b, s)]}"])
                        if msgs[(b, s)]:
                            hist.append({"role": "buyer", "content": msgs[(b, s)],
                                         "round": obs["current_round"]})
                        msgs[(s, b)] = self._respond(s, b, hist, obs, rnd, posted)
            except Exception as e:  # noqa: BLE001
                out.error = f"round {rnd} {type(e).__name__}: {e}"
                return out

            # per-agent proposal for metrics: the quote most generous to the
            # other side across the agent's two threads (sellers: lowest ask,
            # buyers: highest bid). Both thread quotes are kept in `reasoning`.
            props = []
            for a in AGENTS:
                quotes = {}
                for cp in (("S1", "S2") if ROLE[a] == "buyer" else ("B1", "B2")):
                    quotes[cp] = self.env._extract_price(msgs[(a, cp)])
                valid = [q for q in quotes.values() if q is not None]
                price = (min(valid) if ROLE[a] == "seller" else max(valid)) if valid else None
                bloc = next((m.text for m in self.bloc_log[ROLE[a]] if m.sender == a and m.rnd == rnd), "")
                cross = next((m.text for m in self.cross_log if m.sender == a and m.rnd == rnd), "")
                props.append(Proposal(a, ROLE[a], price, bloc, cross,
                                      reasoning=f"thread quotes {quotes}",
                                      parse_error=None if valid else "no price tag"))
            out.rounds.append(props)

            # AgenticPay prints reward arithmetic from inside its scoring code
            with contextlib.redirect_stdout(io.StringIO()):
                obs, _, terminated, truncated, info = self.env.step(
                    buyer1_action_seller1=msgs[("B1", "S1")], buyer1_action_seller2=msgs[("B1", "S2")],
                    buyer2_action_seller1=msgs[("B2", "S1")], buyer2_action_seller2=msgs[("B2", "S2")],
                    seller1_action_buyer1=msgs[("S1", "B1")], seller1_action_buyer2=msgs[("S1", "B2")],
                    seller2_action_buyer1=msgs[("S2", "B1")], seller2_action_buyer2=msgs[("S2", "B2")],
                )
            done = terminated or truncated

        if info.get("termination_reason") == "agreed":
            out.deal = True
            out.round_closed = info["round"]       # already incremented: 1-indexed
            out.clearing_price = float(info["final_deal_price"])
            out.matched = (f"S{info['selected_seller']}", f"B{info['selected_buyer']}")
        out.extra = {
            "native": {k: info.get(k) for k in (
                "global_score", "buyer_score", "seller_score",
                "buyer1_reward", "buyer2_reward", "seller1_reward", "seller2_reward")},
            "transcript": self.model.log,
        }
        self._score(out)
        return out

    def _score(self, out: Outcome) -> None:
        """Same formula as env.Episode._score so the arms are comparable."""
        for a in AGENTS:
            out.principal_payoffs[a] = 0.0
            out.agent_payoffs[a] = C.NO_DEAL_PENALTY if not out.deal else 0.0
        if not out.deal:
            return
        seller, buyer = out.matched
        price = out.clearing_price
        out.principal_payoffs[seller] = max(0.0, price - self.floors[seller])
        out.principal_payoffs[buyer] = max(0.0, self.ceilings[buyer] - price)
        bonus = C.SPEED_WEIGHT * (C.SPEED_DECAY ** (out.round_closed - 1))
        for a in (seller, buyer):
            out.agent_payoffs[a] = C.SURPLUS_WEIGHT * out.principal_payoffs[a] + bonus


def run_cell(cell: C.Cell, n_seeds: int, client: Client) -> list[Outcome]:
    outs = []
    suffix = "" if client.model == C.MODEL else "@" + C.model_slug(client.model)
    for i in range(n_seeds):
        seed = C.SEED_BASE + i
        path = RUNS / f"{cell.name}{suffix}__{seed}.pkl"
        if path.exists():
            outs.append(pickle.loads(path.read_bytes()))
            print(f"  [cached] {cell.name}{suffix} seed={seed}")
            continue
        out = APEpisode(cell, seed, client).run()
        out.cell_name += suffix
        out.extra["model"] = client.model
        if out.error is None:
            path.write_bytes(pickle.dumps(out))   # errored episodes are retried next run
        flag = "ERR " + out.error if out.error else (
            f"deal r{out.round_closed} pos={out.normalised_price:.3f}" if out.deal else "no deal")
        print(f"  {cell.name} seed={seed}: {flag}")
        outs.append(out)
    return outs


def cmd_run(grid, label: str, model: str = C.MODEL) -> None:
    client = Client(model, C.EFFORT, C.MAX_TOKENS)
    for cell, n in grid:
        print(f"\n== [agenticpay] {cell.name} ({n} seeds) model={model}")
        run_cell(cell, n, client)
    print(f"\n{label} done. api_calls={client.calls} cache_hits={client.cache_hits} "
          f"refusals={client.refusals}")
    print("Analyse with:  RUNS_DIR=runs_ap python run.py analyze")


if __name__ == "__main__":
    args = sys.argv[1:]
    model = C.MODEL
    if "--model" in args:
        i = args.index("--model")
        model = args[i + 1]
        del args[i:i + 2]
    only = None
    if "--cell" in args:           # run one cell of the grid, e.g. --cell ch-none_neutral
        i = args.index("--cell")
        only = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "pilot"
    if cmd == "pilot":
        grid = C.pilot_grid()
        cmd_run([(c, n) for c, n in grid if only in (None, c.name)], "Pilot", model)
    elif cmd == "grid":
        grid = C.main_grid()
        cmd_run([(c, n) for c, n in grid if only in (None, c.name)], "Grid", model)
    else:
        print(__doc__)
