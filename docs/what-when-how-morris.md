# Historical Morris: what, when, and how

**Use historical `morris` only to read or reproduce old demonstration provenance.**

**Novice rule: use it only when an existing YAML or plan already says
`method: morris`. Never create a new `morris` campaign.**

**English** | [Français](../docs-fr/what-when-how-morris.md)

## What

Historical `morris` is the old adjacent-level plan. It changes one numeric factor
at a time between neighboring declared values. Its local normalized effects are
noncanonical: they do not match the current Morris definition.

A seed is an integer that selects a simulator random stream. This method uses
seeds as replications of the same factor setting.

This method answers only: "What did this legacy campaign record?" It exists so
old plans and their provenance remain readable. Provenance is the record of a
past run.

## When

Use it when an existing plan or record already has `method: morris` and you must
inspect its historical behavior.

Do not use it for a new campaign. Use [discrete OAT](what-when-how-discrete_oat.md)
for a new adjacent-level demonstration. Use [canonical Morris](what-when-how-canonical_morris.md)
for scientific screening.

## How

The following YAML is a **legacy fragment**. It does not run by itself and is not
a recommendation for a new campaign.

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

There is intentionally no generated complete `morris` starter. See the preserved
[historical YAML schema](yaml-reference.md#historical-morris). For a complete
new demonstration, start from [`02-discrete-oat.yml`](../src/eqasim_sa/templates/02-discrete-oat.yml).
If you must re-analyze an existing legacy configuration, use its original file.
The original project defines `SA` as a Bash command array. `"${SA[@]}"` runs that
command without losing its arguments. See [Getting started](getting-started.md#generated-project-runner).

`plan` creates and seals the plan. `check` validates it and the execution setup.
`estimate` reports the expected resource cost. `run` executes missing runs. `analyze`
creates the result files. Both `check` and `estimate` need resource observations.
Use only observations referenced by the historical configuration. Do not invent
a new calibration for old provenance.

```bash
TARGET=configs/existing-morris.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## What you get

- `indicators.parquet`: one row per completed run and indicator.
- `replication-dispersion.parquet`: variation across replication seeds.
- `morris-ranking.parquet`: noncanonical adjacent-level effects ranked by `mu_star`.
- `output-summary.parquet`: a summary of each indicator over design positions.
- `governed-report.json`: a report with `inferenceStatus: demonstration_noninferential` and `claimAuthorized: false`.
- `analysis-generation.json`: output paths and hashes for this analysis.

Treat the ranking as legacy descriptive evidence. A high rank means that larger
local effects occurred in that old declared design.

## What you may say

You may describe the old factor order and the observed local effects. State that
the effects are noncanonical.

You may not call these canonical Morris effects. You may not infer variance
shares, global importance, factor prioritization, or factor fixing.

## Before you run

- Confirm that this is an existing legacy campaign.
- Preserve the original plan, context, values, and seeds.
- Confirm that all factor domains are numeric and strictly increasing.
- Do not replace old plan bytes by hand.
- Prefer a new method and a new `campaign_root` for new work.

## Continue

- Previous: [Grid](what-when-how-grid.md)
- Next: [Discrete OAT](what-when-how-discrete_oat.md) or [canonical Morris](what-when-how-canonical_morris.md)
- Reference: [YAML](yaml-reference.md#historical-morris), [parameters and methods](parameters-and-methods.md#historical-morris-and-discrete-oat), and [workflow and results](workflow-and-results.md#morris-rankingparquet-historical-compatibility)
