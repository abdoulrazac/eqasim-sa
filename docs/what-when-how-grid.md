# Grid: what, when, and how

**Use Grid for a readable demonstration that compares every declared combination.**

**English** | [Français](../docs-fr/what-when-how-grid.md)

## What

Grid forms every combination of the declared factor values and replication
seeds. A factor is an input that the campaign varies. Grid answers: "What output
do I observe at each declared combination?"

A seed is an integer that selects a simulator random stream. Replication seeds
repeat a combination to show random variation between seeds.

It is a useful baseline because the design is easy to inspect. It also supports
named structured configurations. These configurations give short labels to
ordered lists, such as alternative `matching_attributes` lists.

## When

Use Grid to validate the configuration, execution, storage, and analysis chain.
Use it for a small comparison or as the first design for named structured
configurations.

Do not use Grid to estimate variance shares or make inferential claims. A
variance share is the portion of output variation attributed to an input. An
inferential claim is a conclusion that goes beyond the observed combinations. Do not
use it when the product of all domain sizes and seeds is too large. This product
grows quickly when you add factors or values.

## How

The following YAML is a **fragment**. It does not run by itself.

```yaml
charter:
  status: demonstration
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
```

Start from the complete generated [`01-grid.yml`](../src/eqasim_sa/templates/01-grid.yml).
For ordered list factors, follow the complete [structured-configurations tutorial](structured-configurations-tutorial.md).
The generated project defines `SA` as a Bash command array. `"${SA[@]}"` runs
that command without losing its arguments. See [Getting started](getting-started.md#generated-project-runner).

First run the generated resource calibration as explained in
[Getting started](getting-started.md#first-resource-calibration). `check` and
`estimate` need its runtime and storage observations. Then use these commands.
`plan` creates and seals the plan. `check` validates it and the execution setup.
`estimate` reports the expected resource cost. `run` executes missing runs. `analyze`
creates the result files.

```bash
TARGET=configs/01-grid.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## What you get

- `indicators.parquet`: one row per completed run and indicator.
- `replication-dispersion.parquet`: variation across replication seeds.
- `output-summary.parquet`: a summary of each indicator over design positions.
- `governed-report.json`: a report with `inferenceStatus: demonstration_noninferential` and `claimAuthorized: false`.
- `analysis-generation.json`: output paths and hashes for this analysis.

Read the results as a direct comparison of declared combinations. A large
difference identifies a combination to inspect. It is not a variance share.

## What you may say

You may say that the demonstration compared all declared combinations and that
some combinations produced different observed outputs.

You may not say that Grid proves global importance, supports factor
prioritization, supports factor fixing, or provides inferential evidence.

## Before you run

- Confirm that the campaign is a demonstration.
- Confirm that every factor key exists in the active model configuration.
- Confirm that each scalar candidate has a valid type and can be transported to the model.
- For a structured factor, confirm that its base key is a list.
- Calculate the full number of combinations before planning.
- Use unique values and seeds.
- Run the referenced resource calibration first.
- Use a new `campaign_root` after a design or context change.

## Continue

- Previous: [Getting started](getting-started.md)
- Next: [Discrete OAT](what-when-how-discrete_oat.md)
- Reference: [YAML](yaml-reference.md#grid), [parameters and methods](parameters-and-methods.md#grid), and [workflow and results](workflow-and-results.md)
