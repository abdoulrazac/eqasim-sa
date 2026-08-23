# Discrete OAT: what, when, and how

**Use discrete OAT for descriptive local triage between adjacent numeric levels.**

**English** | [Français](../docs-fr/what-when-how-discrete_oat.md)

## What

OAT means "one factor at a time." The method follows short paths where one
numeric factor changes and all other factors stay fixed. Each change uses two
neighboring declared values.

A seed is an integer that selects a simulator random stream. Replication seeds
repeat each setting to show random variation between seeds.

It answers: "Which local adjacent changes should receive more attention?" This
is descriptive local triage, not scientific screening. Use
[canonical Morris](what-when-how-canonical_morris.md) for scientific screening.
Its effects use local normalized steps. They are not canonical Morris effects.

## When

Use it to check local behavior and triage numeric factors in a demonstration.

Do not use it for text values, named structured configurations, interactions,
variance shares, factor prioritization, or factor fixing. A variance share is the
portion of output variation attributed to an input. Do not use it as a
scientific screening result.

## How

The following YAML is a **fragment**. It does not run by itself.

```yaml
charter:
  status: demonstration
experiment:
  method: discrete_oat
  seed_role: replication
  trajectories: 2
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
```

Start from the complete generated [`02-discrete-oat.yml`](../src/eqasim_sa/templates/02-discrete-oat.yml).
The generated project defines `SA` as a Bash command array. `"${SA[@]}"` runs
that command without losing its arguments. See [Getting started](getting-started.md#generated-project-runner).

First run the generated resource calibration as explained in
[Getting started](getting-started.md#first-resource-calibration). `check` and
`estimate` need its runtime and storage observations. Then use these commands.
`plan` creates and seals the plan. `check` validates it and the execution setup.
`estimate` reports the expected resource cost. `run` executes missing runs. `analyze`
creates the result files.

```bash
TARGET=configs/02-discrete-oat.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## What you get

- `indicators.parquet`: one row per completed run and indicator.
- `replication-dispersion.parquet`: variation across replication seeds.
- `discrete-oat-effects.parquet`: local normalized adjacent-level effects.
- `output-summary.parquet`: a summary of each indicator over design positions.
- `governed-report.json`: a report with `inferenceStatus: demonstration_noninferential` and `claimAuthorized: false`.
- `analysis-generation.json`: output paths and hashes for this analysis.

`mu_star` summarizes effect magnitude. `mu` keeps its direction. `sigma` shows
variation across paths. These values describe only the declared local steps.

## What you may say

You may say that a factor showed large or small local adjacent-level effects in
this demonstration. You may retain a factor for more study.

You may not call this unqualified screening. You may not say that a factor has a
variance share, is globally unimportant, or can be fixed. The method changes one
factor while holding the others fixed. It therefore does not estimate combined
effects between factors, called interactions.

## Before you run

- Confirm that the campaign is a demonstration.
- Use only numeric factors with at least two distinct values.
- Put each factor's values in strict increasing order.
- Check the trajectory count and total design size.
- Run the referenced resource calibration first.
- Use a new `campaign_root` after a design or context change.

## Continue

- Previous: [Grid](what-when-how-grid.md)
- Next: [Canonical Morris](what-when-how-canonical_morris.md)
- Reference: [YAML](yaml-reference.md#discrete-oat), [parameters and methods](parameters-and-methods.md#historical-morris-and-discrete-oat), and [workflow and results](workflow-and-results.md#discrete-oat-effectsparquet)
