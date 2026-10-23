#!/usr/bin/env python3
"""Sell-side contest pilot runner (exploratory, after the freeze).

    python run_contest.py pilot            # 8 cells x 5 seeds (see contest_config.pilot_grid)
    python run_contest.py pilot --cell contest_k1_ch-none   # one cell, for parallel processes
    python run_contest.py firm             # Study C: 7 cells x 5 seeds (contest_config.firm_grid)
    python run_contest.py firm --model openrouter/openai/gpt-4o-2024-11-20   # comparison model
    python run_contest.py buy              # Study C mirrored to the buy side (contest_config.firm_grid_buy)
    python run_contest.py shift-sell / shift-buy   # shifted-range control (+100), contest_config.shifted_grids
    python run_contest.py analyze          # per-cell table and per-episode rows
    python run_contest.py pilot --model claude-opus-5-5   # rows tagged @<slug>

Shares ./cache with the main grid; cell names start with "contest_", so no
cache key collides with a main-grid turn.
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from pathlib import Path

import config as C
import contest_config as K
import contest_metrics as M
from contest_env import ContestEpisode, ContestOutcome
from llm import Client

RUNS = Path(__file__).parent / os.environ.get("RUNS_DIR", "runs_contest")
RUNS.mkdir(exist_ok=True)


def run_cell(cell: K.ContestCell, n_seeds: int, client: Client,
             rival_client: Client | None = None) -> list[ContestOutcome]:
    outs = []
    suffix = "" if client.model == K.MODEL else "@" + C.model_slug(client.model)
    for i in range(n_seeds):
        seed = K.SEED_BASE + i
        path = RUNS / f"{cell.name}{suffix}__{seed}.pkl"
        if path.exists():
            outs.append(pickle.loads(path.read_bytes()))
            print(f"  [cached] {cell.name}{suffix} seed={seed}")
            continue
        out = ContestEpisode(cell, seed, client, rival_client).run()
        out.cell_name += suffix
        out.extra["model"] = client.model
        if out.error is None:
            path.write_bytes(pickle.dumps(out))   # errored episodes are retried next run
        flag = "ERR " + out.error if out.error else (
            f"close r{out.closed_round} awarded={out.awarded} "
            f"gap={M.partner_gap_mean(out)} alloc={M.allocation(out)}")
        print(f"  {cell.name} seed={seed}: {flag}")
        outs.append(out)
    return outs


def load_all() -> dict[str, list[ContestOutcome]]:
    by_cell: dict[str, list[ContestOutcome]] = {}
    for p in sorted(RUNS.glob("*.pkl")):
        o = pickle.loads(p.read_bytes())
        by_cell.setdefault(o.cell_name, []).append(o)
    return by_cell


def cmd_run(grid, label: str, model: str) -> None:
    client = Client(model, K.EFFORT, K.MAX_TOKENS)
    rivals: dict[str, Client] = {}
    for cell, n in grid:
        rc = None
        if cell.rival_model:
            rc = rivals.setdefault(cell.rival_model,
                                   Client(cell.rival_model, K.EFFORT, K.MAX_TOKENS))
        print(f"\n== {cell.name} ({n} seeds) model={model}"
              + (f" independents={cell.rival_model}" if rc else ""))
        run_cell(cell, n, client, rc)
    print(f"\n{label} done. api_calls={client.calls} cache_hits={client.cache_hits} "
          f"refusals={client.refusals}")
    for m, rc in rivals.items():
        print(f"  independents {m}: api_calls={rc.calls} cache_hits={rc.cache_hits} "
              f"refusals={rc.refusals}")
    cmd_analyze()


def cmd_analyze() -> None:
    by_cell = load_all()
    if not by_cell:
        print("No contest runs on disk yet.")
        return
    summaries = [M.summarise(outs) for _, outs in sorted(by_cell.items())]
    print(M.table(summaries))
    print("\nPer episode:")
    rows = []
    for name, outs in sorted(by_cell.items()):
        for o in outs:
            row = {
                "cell": name, "seed": o.seed, "closed_round": o.closed_round,
                "awarded": o.awarded, "allocation": M.allocation(o),
                "gap": M.partner_gap_mean(o), "undercut_rate": M.undercut_rate(o),
                "leapfrog": M.leapfrog_rate(o),
                "position": M.win_position(o), "markup": M.markup(o),
                "descent": M.ask_descent(o), "buyer_surplus": o.buyer_surplus,
                "parse_errors": o.n_parse_errors, "error": o.error,
                "a_wins": sum(s in o.blocs.get("A", ()) for s, _ in o.awarded),
            }
            if "reveal" in name:
                rr = int("".join(ch for ch in name.split("reveal")[1][:3] if ch.isdigit()))
                row["reveal"] = M.reveal_split(o, rr)
            rows.append(row)
            print("  " + json.dumps(row, default=str))
    (RUNS / "summary.json").write_text(json.dumps(
        {"cells": [s.__dict__ for s in summaries], "episodes": rows}, indent=2, default=str))
    print(f"\nWrote {RUNS / 'summary.json'}")


if __name__ == "__main__":
    args = sys.argv[1:]
    model = K.MODEL
    if "--model" in args:
        i = args.index("--model")
        model = args[i + 1]
        del args[i:i + 2]
    only = None
    if "--cell" in args:
        i = args.index("--cell")
        only = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "analyze"
    if cmd in ("pilot", "firm", "buy", "shift-sell", "shift-buy"):
        full = {"pilot": K.pilot_grid, "firm": K.firm_grid, "buy": K.firm_grid_buy,
                "shift-sell": lambda: K.shifted_grids()["sell"],
                "shift-buy": lambda: K.shifted_grids()["buy"]}[cmd]()
        grid = [(c, n) for c, n in full if only in (None, c.name)]
        if not grid:
            raise SystemExit(f"No cell named {only}")
        cmd_run(grid, {"pilot": "Contest pilot", "firm": "Study C (firms)", "buy": "Study C (buy side)",
                       "shift-sell": "Shift control (sell)", "shift-buy": "Shift control (buy)"}[cmd], model)
    else:
        cmd_analyze()
