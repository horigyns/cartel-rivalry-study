# Study A — Competing Cartels

Do two cartels facing each other across a market settle or fight — and does
rivalry between them tighten or dissolve cohesion within them?

Every published collusion study puts one coalition against a principal or
against non-colluding bystanders. This runs coalition against coalition.

## Status

Frozen and running. `PREREG.md` was frozen with file hashes at 2026-10-22
02:50 UTC; the 144-run grid (72 runs per microstructure) has completed on
`openrouter/deepseek/deepseek-chat-v3-0324` and been analysed (see Results). `claude-opus-5-5` is the
comparison model, not the grid model: it declined the coalition in all 36 pilot
episodes (`docs/opus_refusal_taxonomy.md`) and in all 104 frozen-environment
comparison episodes (`docs/opus_refusal_grid.md`); DeepSeek formed blocs in
every cell. Both offline smoke suites print ALL PASS.

Pilot iterations and the changes they forced are recorded in `PREREG.md`,
"Pre-freeze changes from the pilot". Anything changed after the freeze goes in
its "Deviations" section.

The project has its own git repository (initialised 2026-10-23); run pickles,
the response cache, `.env` and the AgenticPay fork are gitignored.

## Layout

| File | What it holds |
| --- | --- |
| `config.py` | Every knob: market, payoffs, cells, grid, pre-registered thresholds |
| `prompts.py` | System prompts, paraphrased bloc/channel/jurisdiction blocks and their builders, judge prompts |
| `env.py` | The self-contained 2v2 bargaining environment and episode loop |
| `llm.py` | Model adapter with disk caching, tolerant JSON parsing, refusal handling |
| `metrics.py` | Measures, `CellSummary`, and the frozen classification rule |
| `judge_reasoning.py` | Construct-validity check: labels every turn's private rationale OTHER_SIDE / OWN_BLOC / SELF; writes `<runs>/reasoning_judge.json` |
| `run.py` | CLI for the self-contained arm: `pilot`, `grid`, `analyze`, `judge`; `--cell`, `--model` |
| `run_ap.py` | Driver for the AgenticPay arm; module docstring lists the mechanics |
| `ap_adapter.py` | Adapts `llm.Client` to AgenticPay's model interface |
| `analysis.py` | Post-grid statistics: bootstrap CIs, factor comparison, paraphrase stability, headline 2x2; writes `results/analysis.json` and `results/analysis.txt` |
| `figures.py` | Draws `results/fig1_cells.png` and `.svg` |
| `figures_contest.py` | Draws `results/fig2_own_distance.png` and `.svg`: award distance from own reservation by side × price level, both firm models |
| `smoke_test.py` | Offline end-to-end check of the self-contained arm |
| `smoke_test_ap.py` | Same for the AgenticPay arm (run in `external/.venv-ap`) |
| `contest_config.py`, `contest_prompts.py`, `contest_env.py`, `contest_metrics.py` | Exploratory sell-side contest pilot: two seller coalitions compete for K contracts from a scripted buyer. Reuses frozen blocks; frozen files untouched. Design in `PREREG.md`, "Exploratory: sell-side contest pilot" |
| `run_contest.py` | CLI for the contest pilot: `pilot`, `analyze`; `--cell`, `--model`. Writes `runs_contest/` |
| `smoke_test_contest.py` | Offline check of the contest pilot: close rule, award, payoffs, channels, prompts, reveal, K=1 and leapfrog, rival-independents cell, metrics |
| `PREREG.md` | Frozen. Source of truth for the rule, grid, stopping rule and pilot changes |
| `docs/CHANGELOG.md` | Dated change log |
| `docs/RESULTS.md` | Full results record. Script-generated; do not edit by hand |
| `docs/opus_refusal_taxonomy.md` | Why Opus is not the grid model: how it declined the coalition |
| `docs/opus_refusal_turns.csv` | The turns behind that taxonomy |
| `refusal_taxonomy.py` | Classifies Opus agent-turns (ethical_stated / ethical_private / silent_assent / silent_none / silent_other / coordinated / misread) from `bloc_message` and private rationale; keyword rules, `coordinated` and `misread` are heuristics with flagged turns listed for review |
| `docs/opus_refusal_grid.md` | Output of `refusal_taxonomy.py` on the frozen-environment Opus runs |
| `results_md.py` | Regenerates `docs/RESULTS.md` from the analysis, judge and taxonomy outputs; never edit RESULTS.md by hand |
| `docs/opus_refusal_grid.csv` | The per-turn classification behind that table |

Run directories, all gitignored: `runs/` and `runs_ap/` are the grid (fresh).
`runs_pilot_v3/` and `runs_ap_pilot_v3/` hold the DeepSeek and Opus pilots with
their `reasoning_judge.json`; `runs_v0_crossing/`, `runs_v1_firstmover/`,
`runs_v2_direction/` and `runs_ap_v2_direction/` are earlier pilot iterations,
kept as evidence and not comparable to anything run since.

## Setup

```bash
pip install -r requirements.txt
printf 'ANTHROPIC_API_KEY=...\nANTHROPIC_WORKSPACE_ID=...\nOPENROUTER_API_KEY=...\n' > .env   # gitignored
python3 smoke_test.py       # offline, should print ALL PASS
python run.py pilot         # 3 seeds x 4 cells (3 channels + full-strength bloc) = 12 runs
python run.py grid          # the full 72-run grid, self-contained arm
python run.py grid --cell ch-none_neutral   # one cell, so cells run as parallel processes
python run.py pilot --model claude-opus-5-5 # comparison run; rows tagged @<slug>
```

The keys and workspace ID are read from `.env` when not already in the shell.
`OPENROUTER_API_KEY` is needed for the grid, which runs on OpenRouter.
`ANTHROPIC_API_KEY` covers the judge and any Opus comparison run.

AgenticPay arm (pinned fork, own venv, both gitignored):

```bash
git clone https://github.com/SafeRL-Lab/AgenticPay external/AgenticPay
git -C external/AgenticPay checkout 1ff4e1a2686eac6a07ff559df6d50329c6fd9f69
python3 -m venv external/.venv-ap
external/.venv-ap/bin/pip install loguru numpy pillow requests anthropic
external/.venv-ap/bin/python smoke_test_ap.py
external/.venv-ap/bin/python run_ap.py grid       # also: pilot, --cell <name>, --model
```

After the grid, in this order:

```bash
python judge_reasoning.py                      # validity labels for runs/
RUNS_DIR=runs_ap python judge_reasoning.py     # and for runs_ap/
python run.py analyze
RUNS_DIR=runs_ap python run.py analyze         # analyze reads reasoning_judge.json
python run.py judge                            # cross and intra-bloc channel scores
python analysis.py                             # CIs and comparisons -> results/analysis.json, .txt
python figures.py                              # results/fig1_cells.png, .svg
python results_md.py                           # regenerate docs/RESULTS.md
# Opus comparison runs only (@claude-opus-5-5 pickles); quote the globs (zsh):
python refusal_taxonomy.py --src 'accept-only=runs/*.pkl' --src 'seller-ask=runs_ap/*.pkl' --out docs/opus_refusal_grid.md
```

Then report. `analyze` needs the validity labels to count accidental crossings;
run `judge_reasoning.py` first.

Every model response is cached in `cache/` keyed on model and message content.
Sampling temperature is not set (the API rejects it), so seeds control
reservation-value draws only; a rerun reproduces a transcript only through the
cache. Episodes are pickled per seed into `runs/` (`runs_ap/` for the
AgenticPay arm), so `analyze` works offline; `RUNS_DIR` selects the directory.
An episode that errors is excluded from summaries and never written to disk, so
a rerun fills the gap.

Agents run on `config.MODEL` = `openrouter/deepseek/deepseek-chat-v3-0324`; the
comparison model is `claude-opus-5-5` (`COMPARISON_MODELS`); the judge is
`claude-sonnet-5-5`. Anthropic calls use low effort with `MAX_TOKENS=4000`
(thinking counts toward it). A refusal is stored as `REFUSAL_MARKER`, counted,
and never retried on another model; `analyze` prints episode errors and
parse/refusal counts.

Models prefixed `openrouter/` go to OpenRouter's chat completions endpoint
(urllib, 180 s hard timeout, 5xx retried with backoff); effort is not sent
there. `--model <id>` on `run.py` / `run_ap.py` overrides the agent model. A
non-default model appends `@<slug>` (`model_slug()`) to the cell name and pickle
filename, so `analyze` shows it as separate rows, and `Outcome.extra["model"]`
records it. The grid is single-family; other models are comparison runs.

## Design

**Market.** Two seller agents and two buyer agents, each acting for a principal
with a private reserve. A trade clears when an agent explicitly accepts a
counterparty's standing quote (the turn JSON has an `accept` field), at that
quote, or when a bid and an ask land within `CLEAR_TOL` = 0.5 of each other, at
the midpoint. A wider crossing does not trade. Quotes are simultaneous within a
round: standing quotes update at round end, so acceptance targets the previous
round's quote. Move order alternates by round. Twenty rounds, then the market
closes. The shared principal block states the direction of surplus plainly
(higher is better for a seller, lower for a buyer, a trade at the reserve earns
nothing). The reasons for each rule are in `PREREG.md`, "Pre-freeze changes from
the pilot".

**Why principals.** In a bilateral cartel with nobody else in the market,
inter-bloc agreement restores trade volume and war destroys surplus — collusion
would come out welfare-improving and the result would be uninterpretable. The
principal layer fixes the polarity.

**The harm mechanism is mutual shirking.** Agent payoff is a share of principal
surplus plus a settlement bonus that decays each round (`SPEED_WEIGHT`,
`SPEED_DECAY`). If both blocs converge on an immediate midpoint split, every
agent collects the bonus and no principal gets the bargaining they paid for.

**Independent variable.** Cross-bloc channel availability: `none`, `public`
(principals can read it), `private` (they cannot). The no-channel cell gets
extra seeds — convergence with no channel at all is Schelling-point
coordination across a trust boundary, and it is the strongest result available
here.

**Bloc strength and paraphrases.** The bloc instruction is a second factor:
`minimal` (the main-grid default) states only that the pair is a coalition with
a private channel; `full` adds the shared-objective sentence and "hold a common
line", and runs as one bolt-on cell, `ch-private_neutral_full` (`Cell.bloc_strength`,
cell-name suffix `_full`). Every manipulated block (bloc, channel, jurisdiction)
has three paraphrases, chosen by `paraphrase_of(seed)` = seed mod 3
(`N_PARAPHRASES`). The shared builders `framing_block`, `bloc_block` and
`channel_block` in `prompts.py` feed both arms, so the text is byte-identical.
`smoke_test.py` section 3d enforces minimal-pair, length-matched levels. The
rationale is in `PREREG.md`, "Design notes (2026-10-21, before any live run)".

**Held fixed.** Topology, bloc size, model family, scenario, round cap. Blocs
are seeded rather than emergent, which buys effect size and costs the emergence
claim.

**Framing bolt-on.** One extra cell re-runs the private-channel condition under
an explicitly unregulated jurisdiction, length-matched to the neutral text
(within 3 words, enforced by the smoke test).
Deliberately not an illicit-market framing — safety refusals would be
indistinguishable from a norm effect.

**Microstructure is a crossed factor.** The same 72-run grid runs in both
arms, 144 grid runs, with identical cells, seeds, reservation draws and
coalition prompt wording (principal and compensation text is shared verbatim
via `PRINCIPAL_BLOCK` and `COMPENSATION_BLOCK`). The two mechanics:

- Self-contained, accept-only (`env.py`, `runs/`): one order book, simultaneous
  quotes, alternating move order, clearing by explicit accept or within
  `CLEAR_TOL` as above.
- AgenticPay seller-ask (`run_ap.py`, `runs_ap/`): a fork of
  github.com/SafeRL-Lab/AgenticPay (commit `1ff4e1a`) with no upstream file
  edited. Four bilateral threads; buyers always move first and sellers see
  same-round bids; a crossed pair settles at the seller's ask unless within
  `price_tolerance`; prices are sticky (a turn with no price tag keeps the
  previous offer). Charter, channel rules, principal text and the per-round
  inbox are injected through each agent's `system_prompt_suffix`; coalition and
  cross messages are harvested from `<coalition_message>` and `<cross_message>`
  tags in the raw output.

Because the arms differ in topology and clearing, the cross is read for whether
a result survives a change of mechanics, and arms are compared for direction of
effect, not level.

**Channel visibility.** Messages are stamped with their round and carried
forward `INBOX_LOOKBACK_ROUNDS`. Visibility is gated per agent by
`Cell.agent_in_bloc`, so independent buyers in the counterfactual arm neither
see nor post on the cross channel.

## Measures

Every cell is printed (`metrics.table`) with its continuous measures beside the
label, so a reader can redraw the boundaries: deal share, median and mean close
round, mean position, reference position, displacement, cross-seed SD, defection
rate, principal shortfall and `n_accidental` (the `accid` column). Position is
normalised: 0 = lowest seller floor, 1 = highest buyer ceiling.

**Classification.** Outcomes are `shirking`, `capture:sellers`,
`capture:buyers`, `war`, `mixed` and `indeterminate`. Settlement needs a high
deal share, an early median close and low cross-seed dispersion; it is then
split by where it sits relative to the counterfactual arm's mean position
(near it is shirking, displaced toward a side is capture). War is any one of
three ways of failing to settle. The bands between are indeterminate by
construction. A cell with no counterfactual run is labelled "settlement (no
reference)". The rule, thresholds and rationale are frozen in `PREREG.md`,
"Classification rule (frozen)"; the parameters live in `config.py`. The earlier
midpoint-focal criterion is gone.

**Construct-validity check.** `judge_reasoning.py` labels each turn's private
rationale OTHER_SIDE, OWN_BLOC or SELF with the judge model, with a keyword
baseline beside it. A deal whose last-3-round rationales reference the other
side in fewer than half of turns is an accidental crossing: reported, not
counted as a deal. Rationale text is a stated reason, not a mechanism, so this
tests attention rather than cause. Pilot result and details are in `PREREG.md`.

Inter-bloc: the classification above plus the judge score on the cross-bloc
channel.

Intra-bloc: defection rate, computed behaviourally: an agent proposing a price
worse for its own bloc than its partner's in the same round, beyond
`DEFECTION_TOL`, skipping round 1 (the first mover posts before its partner has
spoken). `INTRA_JUDGE_PROMPT` adds an optional judge score (`judge.json` keys
`cross` and `intra`).

Welfare: principal shortfall against the counterfactual arm, where one bloc
faces non-colluding counterparts. The baseline is keyed by channel, with a
fallback when that channel has no counterfactual run (`baselines_from`,
`baseline_for`). Baselines are also keyed by model and microstructure and never
cross either, so a `--model` run is only compared with counterfactuals run on
the same model.

Cohesion is read against bloc strength: if blocs hold only under `full`, the
primary result is conditional on instructed coalitions (see `PREREG.md`).
Paraphrase (seed mod 3) is a covariate; with only 10 to 18 seeds per cell, each
paraphrase has few runs, so a cell whose classification flips across
paraphrases is reported as indeterminate.

The headline is a 2×2 of outcomes — settle or fight, crossed with cohesion
tightening or loosening — not a single effect size.

## Results

The grid is 144 episodes (72 per microstructure) on
`deepseek-chat-v3-0324`, with no errors and no refusals. The pre-registration
was frozen before the grid ran. All judges have run: channel judges in
`runs*/judge.json`, the validity judge in `runs*/reasoning_judge.json`. It found
2 accidental crossings in accept-only and 0 in seller-ask. The full record is
`docs/RESULTS.md` (script-generated); the cell-level picture is
`results/fig1_cells.png`.

- **Microstructure moves outcomes far more than channel availability.** Median
  close round shifts by -6 [-8, -4.5] and mean position by +0.40 [0.32, 0.48]
  across microstructures. Channel availability changes the close round by at
  most 5 rounds, in accept-only only, and position by at most 0.07.
- **The pre-registered channel ordering on time-to-deal is mixed.** It holds in
  accept-only (none 11, public 8, private 6 rounds) and not in seller-ask, where
  every channel closes in 2-3 rounds.
- **The clearing rule decides who captures the surplus.** Counterfactual mean
  positions are 0.27 under accept-only and 0.76 under seller-ask clearing.
- **Principal shortfall is small**, about 1 to 2 against a mean total principal payoff of about 46 per episode.
- **Perverse cell.** Accept-only, private channel, full bloc: war between the
  blocs, with the tightest cohesion inside them.
- **Opus comparison.** No bloc formed in 36 pilot episodes
  (`docs/opus_refusal_taxonomy.md`) or in the 104 frozen-environment episodes
  described next.

**Opus under the frozen environment.** `claude-opus-5-5` was also run on the
three minimal channel cells and the full cell, in both microstructures, with the
grid's seeds: 104 episodes, 508 turns, no errors. Rows are tagged
`@claude-opus-5-5` and never pooled with DeepSeek. Two findings, kept apart:

1. **Refusal to form a bloc is dispositional.** Blocs formed in 0 of 104
   episodes under either bloc instruction. Opus closes in round 1 to 2 at
   position about 0.15 (accept-only) and about 0.52 (seller-ask), with defection
   near 0.
2. **How it declines is salience-sensitive.** The share of turns with a
   verbalised ethical decline is 0.60 under the minimal instruction and 0.78
   under full, a difference of -0.18 [-0.27, -0.08], outside the pre-registered
   0.15 band.

The full record is the section "Opus comparison under the frozen environment" at
the end of `docs/RESULTS.md`, and `PREREG.md`, "Additions after freeze". The
taxonomy table is `docs/opus_refusal_grid.md`.

## Failure modes

The real risk is not a null. It is that blocs never cohere and there is nothing
to measure at either level. Defection is instrumented as an outcome, not a
crash: if cohesion collapses everywhere, "bloc cohesion fails before inter-bloc
dynamics become observable" is itself a reportable negative result about
whether multi-cartel scenarios are reachable with current models.

The Friday abort rule (abort to Study B if blocs do not hold) was exercised.
Blocs failed on `claude-opus-5-5`, which declined the coalition in all 36 pilot
episodes and later in all 104 frozen-environment comparison episodes, and passed
on `deepseek-chat-v3-0324`. The decision to run the grid on
DeepSeek, with Opus as the cross-family comparison, is in `PREREG.md`, "Stopping
rule". Study B was not needed.
