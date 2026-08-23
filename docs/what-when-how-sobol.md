# Historical Sobol: what, when, and how

**Use historical `sobol` only as a single-scramble demonstration of the workflow.**

**English** | [Français](../docs-fr/what-when-how-sobol.md)

## What

The historical method projects one randomized Sobol low-discrepancy sequence
onto declared factor values. A low-discrepancy sequence spreads design points
evenly. This method uses one randomization, called a scramble, and bootstraps
rows from that one scramble.

It answers: "Does the historical Sobol planning and analysis chain work?" The
generated `03-sobol.yml` is only a demonstration.

A seed is an integer that selects a simulator random stream. Here it is a factor,
not a replication.

The first-order index describes output variation linked to one factor alone.
The total-order index includes that factor and all its interactions. An
interaction is a combined effect of two or more factors.

## When

Use it to run the generated demonstration or to read legacy provenance.
Provenance is the record of a past run.

Do not use it for scientific factor prioritization (FP) or factor fixing (FF).
FP asks what to learn more accurately first. FF asks what may be fixed within a
declared practical tolerance, including interactions. One scramble and a row
bootstrap do not provide the required uncertainty evidence.

## How

The following YAML is a **fragment**. It does not run by itself.

```yaml
charter:
  status: demonstration
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 99
  confidence_level: 0.95
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 4
  design_seed: 20260814
parameters:
  seed:
    values: [1401, 1402]
```

Start from the complete generated [`03-sobol.yml`](../src/eqasim_sa/templates/03-sobol.yml).
The generated project defines `SA` as a Bash command array. `"${SA[@]}"` runs
that command without losing its arguments. See [Getting started](getting-started.md#generated-project-runner).

First run the generated resource calibration as explained in
[Getting started](getting-started.md#first-resource-calibration). `check` and
`estimate` need its runtime and storage observations. Then use these commands.
`plan` creates and seals the plan. `check` validates it and the execution setup.
`estimate` reports the expected resource cost. `run` executes missing runs. `analyze`
creates the result files.

```bash
TARGET=configs/03-sobol.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## What you get

- `indicators.parquet`: one row per completed run and indicator.
- `sobol-indices.parquet`: historical first-order and total-order estimates with row-bootstrap intervals.
- `output-summary.parquet`: a summary of each indicator over design positions.
- `governed-report.json`: a report with `inferenceStatus: legacy_noninferential` and `claimAuthorized: false`.
- `analysis-generation.json`: output paths and hashes for this analysis.

Read the indices only as legacy demonstration output. Do not treat their
intervals as independent-scramble uncertainty evidence.

One randomized sequence cannot show how estimates vary between independently
randomized sequences. Resampling rows from that same sequence, called a row
bootstrap, does not replace independent randomizations.

## What you may say

You may say that the demonstration produced historical Sobol estimates and
validated the analysis chain.

You may not make scientific FP or FF claims. You may not say that row bootstrap
within one scramble replaces several independent scrambles.

## Before you run

- Confirm that the campaign is a demonstration or legacy provenance.
- Confirm that `seed` is a declared integer factor.
- Confirm that `base_sample_size` is a power of two of at least 2.
- Check the planned positions and discrete projection reuse.
- Run the referenced resource calibration first.
- Use multi-scramble Sobol for a new scientific FP or FF campaign.

## Continue

- Previous: [Grid](what-when-how-grid.md)
- Next: [Multi-scramble Sobol](what-when-how-multiscramble_sobol.md)
- Reference: [YAML](yaml-reference.md#historical-sobol), [parameters and methods](parameters-and-methods.md#historical-sobol), and [workflow and results](workflow-and-results.md#sobol-indicesparquet)
