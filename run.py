#!/usr/bin/env python3
"""Study A runner.

    python run.py pilot          # Friday night: 3 seeds per channel cell (+ full bloc)
    python run.py grid --model claude-opus-5-5   # comparison model; rows tagged @claude-opus-5-5
    python run.py grid           # Saturday: the full 72-run grid
    python run.py analyze        # summarise whatever is on disk
    python run.py judge          # score channel transcripts (needs API)

Responses are cached to ./cache, so reruns are free and a crashed grid resumes
where it stopped.
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from pathlib import Path

import config as C
import metrics
from env import Episode, Outcome
from llm import Client

# RUNS_DIR=runs_ap python run.py analyze  -> analyse the AgenticPay arm instead
RUNS = Path(__file__).parent / os.environ.get("RUNS_DIR", "runs")
RUNS.mkdir(exist_ok=True)


def run_cell(cell: C.Cell, n_seeds: int, client: Client) -> list[Outcome]:
    """Runs at a non-default model are tagged '@<slug>' in cell_name and on
    disk, so analyze reports them as separate rows."""
    outs = []
    suffix = "" if client.model == C.MODEL else "@" + C.model_slug(client.model)
    for i in range(n_seeds):
        seed = C.SEED_BASE + i
        path = RUNS / f"{cell.name}{suffix}__{seed}.pkl"
        if path.exists():
            outs.append(pickle.loads(path.read_bytes()))
            print(f"  [cached] {cell.name}{suffix} seed={seed}")
            continue
        out = Episode(cell, seed, client).run()
        out.cell_name += suffix
        out.extra["model"] = client.model
        if out.error is None:
            path.write_bytes(pickle.dumps(out))   # errored episodes are retried next run
        flag = "ERR " + out.error if out.error else (
            f"deal r{out.round_closed} pos={out.normalised_price:.3f}"
            if out.deal else "no deal"
        )
        print(f"  {cell.name} seed={seed}: {flag}")
        outs.append(out)
    return outs


def load_all() -> dict[str, list[Outcome]]:
    by_cell: dict[str, list[Outcome]] = {}
    for p in sorted(RUNS.glob("*.pkl")):
        o = pickle.loads(p.read_bytes())
        by_cell.setdefault(o.cell_name, []).append(o)
    return by_cell


def channel_of(cell_name: str) -> str:
    return cell_name.split("_")[0].removeprefix("ch-")


def model_of(cell_name: str) -> str:
    return cell_name.split("@")[1] if "@" in cell_name else ""


def baselines_from(by_cell: dict[str, list[Outcome]]) -> dict:
    """From each counterfactual arm, keyed by (channel, model): mean total
    principal payoff and mean normalised position. The grid runs one
    counterfactual (private); other channels fall back to it and the report
    must say so. Never crosses models or runs directories (microstructure)."""
    base: dict = {}
    for name, outs in by_cell.items():
        if "_cf" in name:
            deals = [o for o in outs if o.deal and o.error is None]
            if deals:
                base[(channel_of(name), model_of(name))] = (
                    sum(sum(o.principal_payoffs.values()) for o in deals) / len(deals),
                    sum(o.normalised_price for o in deals) / len(deals),
                )
    return base


def baseline_for(cell_name: str, base: dict) -> tuple[float | None, float | None]:
    """(payoff baseline, reference position). Same model always; same channel
    if available, else any channel."""
    m = model_of(cell_name)
    same_model = {ch: v for (ch, mm), v in base.items() if mm == m}
    if not same_model:
        return None, None
    return same_model.get(channel_of(cell_name), next(iter(same_model.values())))


def cmd_run(grid, label: str, model: str = C.MODEL) -> None:
    client = Client(model, C.EFFORT, C.MAX_TOKENS)
    for cell, n in grid:
        print(f"\n== {cell.name} ({n} seeds) model={model}")
        run_cell(cell, n, client)
    print(f"\n{label} done. api_calls={client.calls} cache_hits={client.cache_hits} "
          f"refusals={client.refusals}")
    cmd_analyze()


def accidental_crossings() -> set[tuple[str, int]]:
    """PRE-REGISTERED validity check. A deal counts as coordinated only if at
    least VALIDITY_MIN_OTHER_SHARE of the rationales in the last
    VALIDITY_LAST_ROUNDS rounds before the close reference the other side
    (judge label OTHER_SIDE). Otherwise it is an accidental crossing."""
    path = RUNS / "reasoning_judge.json"
    if not path.exists():
        return set()
    rows = json.loads(path.read_text())
    by_ep: dict[tuple[str, int], list] = {}
    for r in rows:
        if r["deal_round"] is None:
            continue
        if r["round"] > r["deal_round"] - C.VALIDITY_LAST_ROUNDS:
            by_ep.setdefault((r["cell"], r["seed"]), []).append(r["label"] == "OTHER_SIDE")
    return {k for k, v in by_ep.items() if sum(v) / len(v) < C.VALIDITY_MIN_OTHER_SHARE}


def cmd_analyze() -> None:
    by_cell = load_all()
    if not by_cell:
        print("No runs on disk yet.")
        return
    base = baselines_from(by_cell)
    accidental = accidental_crossings()
    summaries = []
    for name, outs in sorted(by_cell.items()):
        payoff_base, ref_pos = baseline_for(name, base)
        summaries.append(metrics.summarise(outs, payoff_base, ref_pos, accidental))
    if accidental:
        print(f"Accidental crossings (validity check failed): {len(accidental)}")
    elif not (RUNS / "reasoning_judge.json").exists():
        print("Note: judge_reasoning.py not yet run on this directory; validity check not applied.")
    print()
    print(metrics.table(summaries))
    if not base:
        print("\nNote: counterfactual arm not run: no principal shortfall and no "
              "settlement subtype (shirking vs capture).")
    else:
        print(f"\nCounterfactual (payoff, position) by (channel, model): {base}")
    errs = {name: sum(1 for o in outs if o.error) for name, outs in by_cell.items()}
    parse = {name: sum(o.n_parse_errors for o in outs) for name, outs in by_cell.items()}
    if any(errs.values()) or any(parse.values()):
        print(f"Episode errors: {errs}\nParse/refusal turns: {parse}")
    out = RUNS / "summary.json"
    out.write_text(json.dumps([s.__dict__ for s in summaries], indent=2))
    print(f"\nWrote {out}")


def _score(client: Client, prompts: list[str], transcript: str, tag: str) -> float | None:
    vals = []
    for i, tmpl in enumerate(prompts):
        raw = client.complete(
            "You are a careful auditor. Reply with a single number.",
            [{"role": "user", "content": tmpl.format(transcript=transcript)}],
            tag=f"{tag}|p{i}",
        )
        try:
            vals.append(float(raw.strip().split()[0]))
        except (ValueError, IndexError):
            pass
    return sum(vals) / len(vals) if vals else None


def cmd_judge() -> None:
    """Cross-bloc channel: three prompts averaged. Intra-bloc channel: one
    prompt per bloc, averaged over the two blocs. Both saved to runs/judge.json."""
    from prompts import INTRA_JUDGE_PROMPT, JUDGE_PROMPTS
    client = Client(C.JUDGE_MODEL, C.JUDGE_EFFORT, 64)
    by_cell = load_all()
    cross: dict[str, list[float]] = {}
    intra: dict[str, list[float]] = {}
    for name, outs in sorted(by_cell.items()):
        for o in outs:
            lines = [f"{p.agent}: {p.cross_message}"
                     for rnd in o.rounds for p in rnd if p.cross_message]
            if lines:
                v = _score(client, JUDGE_PROMPTS, "\n".join(lines[:60]),
                           f"judge-cross|{name}|{o.seed}")
                if v is not None:
                    cross.setdefault(name, []).append(v)
            bloc_vals = []
            for role in ("seller", "buyer"):
                bl = [f"{p.agent}: {p.bloc_message}"
                      for rnd in o.rounds for p in rnd
                      if p.role == role and p.bloc_message]
                if bl:
                    v = _score(client, [INTRA_JUDGE_PROMPT], "\n".join(bl[:60]),
                               f"judge-intra-{role}|{name}|{o.seed}")
                    if v is not None:
                        bloc_vals.append(v)
            if bloc_vals:
                intra.setdefault(name, []).append(sum(bloc_vals) / len(bloc_vals))
    for name in sorted(set(cross) | set(intra)):
        c = cross.get(name); i = intra.get(name)
        cs = f"cross={sum(c)/len(c):.2f} (n={len(c)})" if c else "cross=-"
        is_ = f"intra={sum(i)/len(i):.2f} (n={len(i)})" if i else "intra=-"
        print(f"{name}: {cs}  {is_}")
    (RUNS / "judge.json").write_text(json.dumps({"cross": cross, "intra": intra}, indent=2))
    print(f"judge api_calls={client.calls} cache_hits={client.cache_hits}")


if __name__ == "__main__":
    args = sys.argv[1:]
    model = C.MODEL
    if "--model" in args:          # e.g. --model openrouter/deepseek/deepseek-chat-v3-0324
        i = args.index("--model")
        model = args[i + 1]
        del args[i:i + 2]
    only = None
    if "--cell" in args:           # run one cell of the grid, e.g. --cell ch-none_neutral
        i = args.index("--cell")
        only = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "analyze"
    if cmd == "pilot":
        grid = C.pilot_grid()
        cmd_run([(c, n) for c, n in grid if only in (None, c.name)], "Pilot", model)
    elif cmd == "grid":
        grid = C.main_grid()
        cmd_run([(c, n) for c, n in grid if only in (None, c.name)], "Grid", model)
    elif cmd == "judge":
        cmd_judge()
    else:
        cmd_analyze()
