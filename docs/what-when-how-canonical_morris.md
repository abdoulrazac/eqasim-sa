# Canonical Morris: what, when, and how

**Use canonical Morris only for scientific screening that retains factors for more study.**

**English** | [Français](../docs-fr/what-when-how-canonical_morris.md)

## What

Screening identifies factors that must remain under consideration. It does not
show that a factor can be fixed.

Canonical Morris uses ordered paths in unit probability space. Unit probability
space is a common scale from 0 to 1. One factor changes at each path step. Every
factor uses the same step size, called `Delta`, before values are mapped to the
declared values. `levels` is the even number of grid positions on that scale. A
common step means that every factor moves by the same `Delta`.

The current implementation supports only a discrete uniform distribution with
the identity transform. In plain terms, each distinct declared value has the
same probability, and the method does not reshape those values.

The method answers: "Which factors must remain in later study?" It targets the
conditional mean, which is the expected indicator over declared simulator seeds.
A seed is an integer that selects a simulator random stream. This target is the
estimand: the quantity that the study intends to estimate.

## When

Use it only with a scientific charter and independent factors. Independence
means that learning one factor's value gives no information about another's.
Declare an even level count, a common step, and a budget. A checkpoint is a
planned sample size where stability is assessed. A stopping criterion is a rule
declared in advance for deciding whether evidence is stable enough. A robustness
check asks whether the conclusion survives a declared alternative assumption or
setting. Start with a separate pilot. Use the pilot's replication recommendation
in a new production campaign and root.

Do not use it for factor prioritization, factor fixing, variance shares, named
structured configurations, or dependent factors. Do not present a pilot as
production evidence. A variance share is the portion of output variation
attributed to an input.

## How

The following YAML is a **fragment**. It omits required study and execution data
and does not run by itself.

```yaml
charter:
  status: scientific
  decision:
    objective: screening
  uncertainty:
    dependence:
      kind: independent
  stochastic:
    target: conditional_mean
    common_random_numbers: within_trajectory
    allocation:
      phase: pilot
experiment:
  method: canonical_morris
  seed_role: replication
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
  replications:
    seeds: [1301, 1302]
```

The Nantes source files require `render_protocol.py`. They are not generic
templates. Follow the exact [Nantes rendering instructions](../protocols/nantes-scientific/README.md#rendering).
The pilot render requires all machine paths and a new absolute campaign root:

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/01-canonical-morris-pilot.yml rendered/pilot.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/scratch" --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/canonical-morris-pilot"
```

The project defines `SA` as a Bash command array. `"${SA[@]}"` runs that command
without losing its arguments. See [Getting started](getting-started.md#generated-project-runner).
`plan` creates and seals the plan. `check` validates it and the execution setup.
`estimate` reports the expected resource cost. `run` executes missing runs. `analyze`
creates the result files. The Nantes scientific files have no calibration block.
Therefore, first run `plan`. Then obtain a valid external or calibration resource
estimate, or execute explicitly under your site's procedure. Fresh `check` and
`estimate` calls are not promised to work without resource observations.

Execute and analyze the pilot:

```bash
PILOT=rendered/pilot.yml
"${SA[@]}" plan --meta-config "$PILOT"
"${SA[@]}" run --meta-config "$PILOT"
"${SA[@]}" analyze --meta-config "$PILOT"
```

Inspect `replication-allocation.json`. Continue only when its allocation status
is `recommended`. A `capped` or `infeasible` status means stop and revise the
study. Never proceed automatically. The production renderer reads the pilot
evidence through `--pilot-campaign-root`:

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/02-canonical-morris-production.yml rendered/production.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/scratch" --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/canonical-morris-production" \
  --pilot-campaign-root "$ROOT/campaigns/canonical-morris-pilot"
```

Execute and analyze production:

```bash
PRODUCTION=rendered/production.yml
"${SA[@]}" plan --meta-config "$PRODUCTION"
"${SA[@]}" run --meta-config "$PRODUCTION"
"${SA[@]}" analyze --meta-config "$PRODUCTION"
```

For another study, author a complete configuration. There is no generic render
step for arbitrary studies.

## What you get

- `indicators.parquet`: one row per completed run and indicator.
- `replication-dispersion.parquet`: variation across simulator seeds.
- `canonical-morris-ranking.parquet`: final screening ranks and intervals. An interval is a range that shows design uncertainty.
- `canonical-morris-convergence.parquet`: the same evidence at nested checkpoints.
- `canonical-morris-rank-reversals.parquet`: evidence of rank reversals. A rank reversal occurs when resampling swaps two factors' order.
- `noise-floor.parquet`: seed variation and Monte Carlo error. Monte Carlo error is uncertainty caused by using finitely many simulator seeds.
- `replication-allocation.json`: pilot-only recommendation for a new production campaign.
- `output-summary.parquet`, `convergence-evidence.parquet`, `governed-report.json`, and `analysis-generation.json`: summaries, stopping evidence, governed status, paths, and hashes.

Bootstrap means repeatedly resampling whole trajectories to assess finite-design
uncertainty. It does not resample individual seed replications as trajectories.

Read status progression as follows:

- `pilot_only`: use the result only for replication planning.
- `unstable`: draw no screening conclusion.
- `conditional`: make only an internal conditional screening statement.
- Robustness pending or `claimAuthorized: false`: this is not an authorized final decision.

## What you may say

You may say that production screening conditionally retains factors when the
declared stopping criteria pass. You must state the charter and pending
robustness checks.

You may not say that screening proves a factor unimportant, estimates a variance
share, gives an FP or FF result, or authorizes a claim. A pilot supports only
replication planning.

Wide intervals, rank reversals, or failed convergence must remain visible. Use a
new campaign with a larger predeclared budget, or retain the affected factors.
Never hide unstable evidence.

## Before you run

- Declare the decision setting, uncertainty model, estimand, and primary indicator.
- Justify independent factors and each discrete distribution.
- Use an even `levels` value of at least 4 and at least two trajectories.
- Check nested checkpoints, bootstrap count, budget, and stopping criteria.
- Separate pilot and production campaign names and roots.
- Declare robustness checks before planning.

## Continue

- Previous: [Discrete OAT](what-when-how-discrete_oat.md)
- Next: [Multi-scramble Sobol](what-when-how-multiscramble_sobol.md)
- Reference: [YAML](yaml-reference.md#canonical-morris), [parameters and methods](parameters-and-methods.md#canonical-morris), and [workflow and results](workflow-and-results.md#canonical-morris-rankingparquet)
