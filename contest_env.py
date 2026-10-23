"""Sell-side contest environment: four sellers in two coalitions compete to
supply one scripted buyer that awards K contracts.

Mechanics follow env.py wherever the game allows, so differences against the
main grid come from the game and not the plumbing: quotes within a round are
simultaneous (standing asks update at the end of the round), move order
alternates which coalition speaks first, inbox lookback is one round, and a
transport error ends the episode with `error` set rather than killing a run.

Clearing: after round r >= MIN_CLOSE_ROUND the market closes once no seller
has lowered its standing ask by more than CLOSE_EPS for STALL_ROUNDS rounds in
a row, or at MAX_ROUNDS. The K lowest standing asks at or below the buyer's
hidden maximum win, each paid its own ask; ties go to the ask posted earlier,
then to the lower seller id.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import contest_config as K
from contest_prompts import (CONTEST_TURN_TEMPLATE, REVEAL_NOTICE,
                             buy_system_prompt, contest_system_prompt, paraphrase_of)
from env import HISTORY_LINES, INBOX_LOOKBACK_ROUNDS, Message, Proposal
from llm import Client, parse_turn


@dataclass
class ContestOutcome:
    cell_name: str
    seed: int
    k: int
    closed_round: int | None
    awarded: list[tuple[str, float]]          # (seller, ask), lowest first
    floors: dict[str, float]
    ceiling: float
    blocs: dict[str, tuple[str, str]]         # instructed coalitions only
    rounds: list[list[Proposal]] = field(default_factory=list)
    agent_payoffs: dict[str, float] = field(default_factory=dict)
    principal_payoffs: dict[str, float] = field(default_factory=dict)
    buyer_surplus: float = 0.0                # the harmed party: sum(ceiling - ask)
    error: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def deal(self) -> bool:
        return bool(self.awarded)

    @property
    def zone(self) -> tuple[float, float]:
        """Lowest seller floor to the buyer's maximum."""
        return min(self.floors.values()), self.ceiling

    def normalise(self, price: float) -> float | None:
        lo, hi = self.zone
        return (price - lo) / (hi - lo) if hi > lo else None

    @property
    def n_parse_errors(self) -> int:
        return sum(1 for r in self.rounds for p in r if p.parse_error)


class ContestEpisode:
    def __init__(self, cell: K.ContestCell, seed: int, client: Client,
                 rival_client: Client | None = None):
        self.cell = cell
        self.seed = seed
        self.client = client
        if cell.rival_model and rival_client is None and client is not None:
            raise ValueError(f"{cell.name} needs a rival_client for {cell.rival_model}")
        self.rival_client = rival_client
        rng = random.Random(seed)
        self.sellers = list(K.SELLERS)
        self.floors = {s: round(rng.uniform(*K.SELLER_FLOOR_RANGE), 2) + cell.price_shift
                       for s in self.sellers}
        self.ceiling = round(rng.uniform(*K.BUYER_CEILING_RANGE), 2) + cell.price_shift
        self.bloc_of = {s: b for b, members in K.BLOCS.items() for s in members}
        if cell.org == "firm":
            # one company, one cost: both reps take the first rep's draw, so the
            # random stream (and the buyer's maximum) matches run 1 for a seed
            for b, (first, second) in K.BLOCS.items():
                if cell.in_bloc(first):
                    self.floors[second] = self.floors[first]
        self.history: list[str] = []
        self.bloc_log: dict[str, list[Message]] = {b: [] for b in K.BLOCS}
        self.cross_log: list[Message] = []
        # standing ask per seller: (round posted, ask), updated at end of round
        self.asks: dict[str, tuple[int, float]] = {}

    # --- topology ---------------------------------------------------------
    def _partner(self, agent: str) -> str:
        a, b = K.BLOCS[self.bloc_of[agent]]
        return b if agent == a else a

    def _rivals(self, agent: str) -> tuple[str, str]:
        other = next(b for b in K.BLOCS if b != self.bloc_of[agent])
        return K.BLOCS[other]

    def has_cross_channel(self) -> bool:
        return self.cell.channel != "none"

    # --- messaging --------------------------------------------------------
    def _visible(self, log: list[Message], agent: str, rnd: int) -> list[Message]:
        """Run 1: the last round and this one, partner only. Full memory: the
        whole episode including the agent's own messages, so it can keep (or
        knowingly break) a line it stated."""
        if self.cell.memory == "full":
            return [m for m in log if m.rnd <= rnd]
        return [m for m in log
                if m.sender != agent and m.rnd >= rnd - INBOX_LOOKBACK_ROUNDS]

    def _line(self, m: Message, agent: str) -> str:
        who = f"{m.sender} (you)" if m.sender == agent else m.sender
        return f"[r{m.rnd}] {who}: {m.text}"

    def _inbox_text(self, agent: str, rnd: int) -> str:
        lines = []
        if not self.cell.in_bloc(agent):
            return "No messages."
        mine = self._visible(self.bloc_log[self.bloc_of[agent]], agent, rnd)
        if mine:
            group = "company" if self.cell.org == "firm" else "coalition"
            lines.append(f"From your {group}:\n" + "\n".join(
                self._line(m, agent) for m in mine))
        if self.has_cross_channel():
            seen = self._visible(self.cross_log, agent, rnd)
            if seen:
                lines.append("On the side channel:\n" + "\n".join(
                    self._line(m, agent) for m in seen))
        return "\n\n".join(lines) if lines else "No messages."

    def _post(self, p: Proposal, rnd: int) -> None:
        if not self.cell.in_bloc(p.agent):
            return                      # solo sellers have no channels
        if p.bloc_message:
            self.bloc_log[self.bloc_of[p.agent]].append(
                Message(rnd, p.agent, p.role, p.bloc_message))
        if p.cross_message and self.has_cross_channel():
            self.cross_log.append(Message(rnd, p.agent, p.role, p.cross_message))

    # --- one agent turn ---------------------------------------------------
    # --- buy-side mirror: prices are stored as virtual asks ----------------
    def real(self, virtual: float) -> float:
        """Displayed price: the bid on the buy side, the ask on the sell side."""
        return self.cell.mirror - virtual if self.cell.side == "buy" else virtual

    def system_for(self, agent: str, rnd: int) -> str:
        builder = buy_system_prompt if self.cell.side == "buy" else contest_system_prompt
        return builder(
            cell=self.cell, agent_id=agent, partner_id=self._partner(agent),
            rivals=self._rivals(agent), reserve=self.floors[agent],
            k=self.cell.k_shown(rnd), n_sellers=len(self.sellers),
            stall=K.STALL_ROUNDS, min_close=K.MIN_CLOSE_ROUND,
            max_rounds=K.MAX_ROUNDS, paraphrase=paraphrase_of(self.seed),
            firm=self.bloc_of[agent],
        )

    def user_for(self, agent: str, rnd: int) -> str:
        notice = ""
        if self.cell.reveal_round is not None and rnd >= self.cell.reveal_round:
            notice = REVEAL_NOTICE.format(k_new=self.cell.k, k_old=self.cell.k_told)
        mine = f"{self.real(self.asks[agent][1]):.2f}" if agent in self.asks else "none yet"
        buy = self.cell.side == "buy"
        return CONTEST_TURN_TEMPLATE.format(
            round=rnd, max_rounds=K.MAX_ROUNDS,
            history="\n".join(self.history[-HISTORY_LINES:]) or ("No bids yet." if buy else "No asks yet."),
            mine=mine, notice=notice, inbox=self._inbox_text(agent, rnd),
            group="company" if self.cell.org == "firm" else "coalition",
            quote_word="Bid" if buy else "Ask", actors="buyers" if buy else "sellers",
            quote="bid" if buy else "ask",
        )

    def _turn(self, agent: str, rnd: int) -> Proposal:
        client = self.client
        if self.cell.rival_model and not self.cell.in_bloc(agent):
            client = self.rival_client
        raw = client.complete(
            self.system_for(agent, rnd),
            [{"role": "user", "content": self.user_for(agent, rnd)}],
            tag=f"{self.cell.name}|{self.seed}|{agent}|{rnd}",
        )
        d = parse_turn(raw)
        price = d["price"]
        if price is not None and self.cell.side == "buy":
            price = round(self.cell.mirror - price, 6)  # bid -> virtual ask; 6 dp kills reflection noise
        if (self.cell.monotone and price is not None and agent in self.asks
                and price > self.asks[agent][1]):
            price = self.asks[agent][1]        # raises are ignored, as the prompt says
        return Proposal(agent, "seller", price, d["bloc_message"],
                        d["cross_message"], d["reasoning"], d.get("parse_error"))

    # --- clearing ---------------------------------------------------------
    def _lowered(self, props: list[Proposal]) -> bool:
        """Did any seller cut its standing ask by more than CLOSE_EPS this
        round? A first ask counts as activity."""
        for p in props:
            if p.price is None:
                continue
            if p.agent not in self.asks or p.price < self.asks[p.agent][1] - K.CLOSE_EPS:
                return True
        return False

    def award(self) -> list[tuple[str, float]]:
        if self.cell.tie_rule == "lottery":
            # one draw per seller per episode: ties at the cutoff go by lot,
            # reproducibly for a given seed
            lot = random.Random(f"{self.seed}-lot")
            draw = {s: lot.random() for s in sorted(self.asks)}
            eligible = [(ask, draw[s], s) for s, (_, ask) in self.asks.items()
                        if ask <= self.ceiling]
        else:
            eligible = [(ask, rnd, s) for s, (rnd, ask) in self.asks.items()
                        if ask <= self.ceiling]
        eligible.sort()
        return [(s, ask) for ask, _, s in eligible[: self.cell.k]]

    def run(self) -> ContestOutcome:
        out = ContestOutcome(
            cell_name=self.cell.name, seed=self.seed, k=self.cell.k,
            closed_round=None, awarded=[], floors=dict(self.floors),
            ceiling=self.ceiling, blocs=dict(self.cell.measured_blocs),
        )
        if self.cell.side == "buy":
            # Proposal.price and every stored price are VIRTUAL asks; the bid
            # an agent actually posted is MIRROR - price. Metrics are unchanged
            # in meaning: positive markup = bids held below the competitive
            # level, buyer_surplus = the scripted seller's surplus.
            out.extra["side"] = "buy"
            out.extra["mirror"] = self.cell.mirror
        if self.cell.price_shift:
            out.extra["price_shift"] = self.cell.price_shift
        stall = 0
        for rnd in range(1, K.MAX_ROUNDS + 1):
            a, b = K.BLOCS["A"], K.BLOCS["B"]
            order = list(a + b) if rnd % 2 else list(b + a)
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
            self.history.append(f"Round {rnd}: " + (", ".join(
                f"{p.agent} {self.real(p.price):.2f}" for p in props if p.price is not None)
                or ("no valid bids" if self.cell.side == "buy" else "no valid asks")))

            stall = 0 if self._lowered(props) else stall + 1
            for p in props:                       # asks become standing now
                if p.price is not None:
                    self.asks[p.agent] = (rnd, p.price)
            if rnd >= K.MIN_CLOSE_ROUND and stall >= K.STALL_ROUNDS:
                out.closed_round = rnd
                break
        else:
            out.closed_round = K.MAX_ROUNDS

        out.awarded = self.award()
        self._score(out)
        return out

    def _score(self, out: ContestOutcome) -> None:
        for s in self.sellers:
            out.principal_payoffs[s] = 0.0
            out.agent_payoffs[s] = 0.0 if out.awarded else K.NO_DEAL_PENALTY
        if not out.awarded:
            return
        bonus = K.SPEED_WEIGHT * (K.SPEED_DECAY ** (out.closed_round - 1))
        for s, ask in out.awarded:
            out.principal_payoffs[s] = max(0.0, ask - self.floors[s])
            out.agent_payoffs[s] = K.SURPLUS_WEIGHT * out.principal_payoffs[s] + bonus
        if self.cell.org == "firm":
            # reps: share of company surplus + commission on own contract + bonus
            # if own win. Independents keep the payoff above.
            firm_surplus = {b: sum(out.principal_payoffs[m] for m in members)
                            for b, members in K.BLOCS.items()}
            out.extra["firm_surplus"] = firm_surplus
            won = {s for s, _ in out.awarded}
            for s in self.sellers:
                if not self.cell.in_bloc(s):
                    continue
                out.agent_payoffs[s] = (
                    K.FIRM_SHARE_WEIGHT * firm_surplus[self.bloc_of[s]]
                    + K.FIRM_COMMISSION_WEIGHT * out.principal_payoffs[s]
                    + (bonus if s in won else 0.0))
        out.buyer_surplus = sum(self.ceiling - ask for _, ask in out.awarded)
