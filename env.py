"""Bilateral bargaining environment: two seller agents against two buyer
agents, each acting for a principal with a private reserve.

Self-contained on purpose. If you later want AgenticPay's richer market
configurations, replace Episode.run while keeping the Outcome shape - metrics.py
only depends on Outcome.

Message flow: every message is stamped with the round it was sent in and kept
for the whole episode. An agent's inbox shows messages from the previous round
and from earlier in the current round, so the first mover in a coalition still
hears its partner, and sellers hear buyers on the cross channel. Visibility is
gated per agent, not per cell: an agent with no coalition (counterfactual arm)
neither sees nor posts on the cross channel, matching what its prompt says.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import config as C
from llm import Client, parse_turn
from prompts import TURN_TEMPLATE, paraphrase_of, system_prompt

# How many previous rounds of messages an agent sees. 1 = last round plus the
# current one. Raising this lengthens prompts without changing the design.
INBOX_LOOKBACK_ROUNDS = 1
HISTORY_LINES = 12


@dataclass
class Proposal:
    agent: str
    role: str
    price: float | None
    bloc_message: str
    cross_message: str
    reasoning: str
    parse_error: str | None = None
    accept: str | None = None      # id of a counterparty whose quote is accepted


@dataclass
class Message:
    rnd: int
    sender: str
    role: str
    text: str


@dataclass
class Outcome:
    cell_name: str
    seed: int
    deal: bool
    round_closed: int | None
    clearing_price: float | None
    matched: tuple[str, str] | None
    seller_floors: dict[str, float]
    buyer_ceilings: dict[str, float]
    rounds: list[list[Proposal]] = field(default_factory=list)
    agent_payoffs: dict[str, float] = field(default_factory=dict)
    principal_payoffs: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    # Arm-specific extras, e.g. AgenticPay's native scores. Never read by metrics.
    extra: dict = field(default_factory=dict)

    @property
    def zone(self) -> tuple[float, float]:
        """Widest bargaining zone in the market: lowest floor to highest ceiling."""
        return min(self.seller_floors.values()), max(self.buyer_ceilings.values())

    @property
    def normalised_price(self) -> float | None:
        """0.0 = lowest seller floor, 1.0 = highest buyer ceiling, 0.5 = focal."""
        if self.clearing_price is None:
            return None
        lo, hi = self.zone
        return (self.clearing_price - lo) / (hi - lo) if hi > lo else None

    @property
    def n_parse_errors(self) -> int:
        return sum(1 for r in self.rounds for p in r if p.parse_error)


class Episode:
    def __init__(self, cell: C.Cell, seed: int, client: Client):
        self.cell = cell
        self.seed = seed
        self.client = client
        rng = random.Random(seed)

        self.sellers = [f"S{i+1}" for i in range(C.N_SELLERS)]
        self.buyers = [f"B{i+1}" for i in range(C.N_BUYERS)]
        self.floors = {
            s: round(rng.uniform(*C.SELLER_FLOOR_RANGE), 2) for s in self.sellers
        }
        self.ceilings = {
            b: round(rng.uniform(*C.BUYER_CEILING_RANGE), 2) for b in self.buyers
        }
        self.history: list[str] = []
        self.bloc_log: dict[str, list[Message]] = {"seller": [], "buyer": []}
        self.cross_log: list[Message] = []
        # standing quote per agent: (round, price). Updated at the END of each
        # round, so quotes within a round are simultaneous: nobody sees the
        # other side's current quote before posting, and acceptance targets the
        # previous round's quote. (The pilot showed first movers opening near
        # their own reserve and later movers anchoring to it.)
        self.quotes: dict[str, tuple[int, float]] = {}

    # --- topology ---------------------------------------------------------
    def _role(self, agent: str) -> str:
        return "seller" if agent in self.sellers else "buyer"

    def _partner(self, agent: str) -> str | None:
        pool = self.sellers if agent in self.sellers else self.buyers
        others = [a for a in pool if a != agent]
        return others[0] if others else None

    def in_bloc(self, agent: str) -> bool:
        return self.cell.agent_in_bloc(self._role(agent)) and self._partner(agent) is not None

    def has_cross_channel(self, agent: str) -> bool:
        """Only coalition members get the cross-bloc channel; independent agents
        are told there is no channel, so they must not see or post on it."""
        return self.cell.channel != "none" and self.in_bloc(agent)

    # --- messaging --------------------------------------------------------
    def _visible(self, log: list[Message], agent: str, rnd: int) -> list[Message]:
        return [m for m in log
                if m.sender != agent and m.rnd >= rnd - INBOX_LOOKBACK_ROUNDS]

    def _inbox_text(self, agent: str, rnd: int) -> str:
        role = self._role(agent)
        lines = []
        if self.in_bloc(agent):
            mine = self._visible(self.bloc_log[role], agent, rnd)
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

    def _post(self, p: Proposal, rnd: int) -> None:
        if p.bloc_message and self.in_bloc(p.agent):
            self.bloc_log[p.role].append(Message(rnd, p.agent, p.role, p.bloc_message))
        if p.cross_message and self.has_cross_channel(p.agent):
            self.cross_log.append(Message(rnd, p.agent, p.role, p.cross_message))

    # --- one agent turn ---------------------------------------------------
    def _counterparties(self, agent: str) -> list[str]:
        return self.buyers if agent in self.sellers else self.sellers

    def _standing_text(self, agent: str) -> str:
        lines = []
        for c in self._counterparties(agent):
            if c in self.quotes:
                r, p = self.quotes[c]
                lines.append(f"{c}: {p:.2f} (round {r})")
        return "\n".join(lines) if lines else "None yet."

    def _turn(self, agent: str, rnd: int) -> Proposal:
        role = self._role(agent)
        reserve = self.floors[agent] if role == "seller" else self.ceilings[agent]
        sys = system_prompt(
            cell=self.cell,
            role=role,
            agent_id=agent,
            partner_id=self._partner(agent),
            reserve=reserve,
            n_sellers=C.N_SELLERS,
            n_buyers=C.N_BUYERS,
            max_rounds=C.MAX_ROUNDS,
            paraphrase=paraphrase_of(self.seed),
            clear_tol=C.CLEAR_TOL,
        )
        user = TURN_TEMPLATE.format(
            round=rnd,
            max_rounds=C.MAX_ROUNDS,
            history="\n".join(self.history[-HISTORY_LINES:]) or "No quotes yet.",
            standing=self._standing_text(agent),
            inbox=self._inbox_text(agent, rnd),
        )
        raw = self.client.complete(sys, [{"role": "user", "content": user}],
                                   tag=f"{self.cell.name}|{self.seed}|{agent}|{rnd}")
        d = parse_turn(raw)
        accept = d.get("accept")
        if accept not in self._counterparties(agent) or accept not in self.quotes:
            accept = None   # can only accept an opposite-side agent with a standing quote
        return Proposal(agent, role, d["price"], d["bloc_message"],
                        d["cross_message"], d["reasoning"], d.get("parse_error"), accept)

    # --- clearing ---------------------------------------------------------
    def _match(self, props: list[Proposal]) -> tuple[tuple[str, str], float] | None:
        """Explicit acceptance first, in move order, at the accepted quote.
        Otherwise the closest bid-ask pair within CLEAR_TOL, at the midpoint.
        A crossing wider than CLEAR_TOL does not trade."""
        for p in props:
            if p.accept:
                price = self.quotes[p.accept][1]
                pair = (p.agent, p.accept) if p.role == "seller" else (p.accept, p.agent)
                return pair, price
        best = None
        for p in props:
            if p.role != "seller" or p.price is None:
                continue
            for q in props:
                if q.role != "buyer" or q.price is None:
                    continue
                gap = abs(q.price - p.price)
                if gap <= C.CLEAR_TOL and (best is None or gap < best[2]):
                    best = ((p.agent, q.agent), (p.price + q.price) / 2, gap)
        return (best[0], best[1]) if best else None

    def run(self) -> Outcome:
        out = Outcome(
            cell_name=self.cell.name, seed=self.seed, deal=False, round_closed=None,
            clearing_price=None, matched=None,
            seller_floors=dict(self.floors), buyer_ceilings=dict(self.ceilings),
        )
        for rnd in range(1, C.MAX_ROUNDS + 1):
            # alternate which side moves first, so neither side always speaks
            # first on the channels
            order = (self.sellers + self.buyers) if rnd % 2 else (self.buyers + self.sellers)
            props: list[Proposal] = []
            for agent in order:
                try:
                    p = self._turn(agent, rnd)
                except Exception as e:  # noqa: BLE001 - one bad call must not kill a grid
                    out.error = f"{type(e).__name__}: {e}"
                    return out
                props.append(p)
                self._post(p, rnd)

            out.rounds.append(props)
            quotes = ", ".join(
                f"{p.agent} {p.price:.2f}" + (f" (accepts {p.accept})" if p.accept else "")
                for p in props if p.price is not None
            )
            self.history.append(f"Round {rnd}: {quotes or 'no valid quotes'}")

            m = self._match(props)
            if m:
                (seller, buyer), price = m
                out.deal, out.round_closed = True, rnd
                out.clearing_price, out.matched = price, (seller, buyer)
                break
            for p in props:                       # quotes become standing now
                if p.price is not None:
                    self.quotes[p.agent] = (rnd, p.price)

        self._score(out)
        return out

    def _score(self, out: Outcome) -> None:
        for a in self.sellers + self.buyers:
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
