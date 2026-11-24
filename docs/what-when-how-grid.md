# Grid: compare a few settings step by step

**Practical example: check whether `sampling_rate` changes the model outputs.**

**English** | [Français](../docs-fr/what-when-how-grid.md)

## At a glance

Suppose you are choosing between two sampling rates: `0.001` and `0.002`. Grid
runs each rate with several simulator seeds. You can then compare the outputs
between the two rates and the variation between seeds.

Grid is the simplest method to start with because it tests every combination
you specify. It answers a descriptive question:

> What did we observe for each setting we tested?

It does not answer "what share of the uncertainty comes from each parameter?"
and does not prove that a parameter is important in every situation.

## Four useful terms

- A **parameter** is an input that you vary, here `sampling_rate`.
- A **seed** is an integer that controls a random draw in the simulator.
- An **output** is a table produced by eqasim, such as the age distribution.
- An **indicator** summarizes an output as a number that can be compared across runs.

In this guide, the indicator is total variation distance, abbreviated TV. It
compares a synthetic distribution with a reference: `0` means that they are
identical, and `1` means that they do not overlap. A lower value therefore means
closer agreement for this indicator, but it does not by itself summarize the
model's overall quality.

## End-to-end practical example

### 1. Ask the question

Question: "Does changing `sampling_rate` from `0.001` to `0.002` alter the
synthetic distributions of age, travel distance, and number of vehicles?"

The `0.001` rate samples about 0.1% of the population, and `0.002` samples about
0.2%. Increasing this rate may reduce sampling noise, but it also increases
runtime. We keep the model, data, and other parameters fixed so that the
comparison remains easy to understand.

This small campaign is still a **demonstration**. It validates the workflow and
shows what happened for these two values; it is not a scientific sensitivity
analysis.

### 2. Generate the starter project

First follow the [installation instructions](getting-started.md#install-with-uv),
then run the following commands from the `openmole-eqasim` root:

```bash
"${SA[@]}" init \
  --project-name mon-analyse-sa \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
cd mon-analyse-sa
```

`init` creates a **rendered, ready-to-use** file at `configs/01-grid.yml`. The
source file `src/eqasim_sa/templates/01-grid.yml` is only a template containing
placeholders; do not run it directly.

### 3. Declare the parameter and replications

To isolate the effect of `sampling_rate`, derive a new campaign from the
generated file and give it a new name:

```bash
cp configs/01-grid.yml configs/01-grid-sampling-rate.yml
```

In `configs/01-grid-sampling-rate.yml`, replace the corresponding blocks with:

```yaml
campaign: mon-analyse-sa-grid-sampling-rate
experiment:
  method: grid
  seeds: [1201, 1202, 1203]
parameters:
  sampling_rate: [0.001, 0.002]
```

Compared with the starter, only the campaign name and the third seed change.
Leave the rest of the generated file unchanged, including `outputs`, `calibration`, `model`,
and `execution`. The new `campaign` name also gives the campaign its own folder,
`campaigns/mon-analyse-sa-grid-sampling-rate/`. To check the complete
configuration, defaults included, run
`"${SA[@]}" show-config configs/01-grid-sampling-rate.yml`.

Your eqasim configuration must contain `config.sampling_rate` (the official
`config.yml` does) and list `analysis.synthesis.population` in `run:` (see
[Prepare your eqasim configuration](getting-started.md#prepare-your-eqasim-configuration)).
The seeds are not
`sampling_rate` values: they repeat each rate to reveal random variation. This
design contains:

```text
2 rates × 3 seeds = 6 positions and 6 unique runs
```

### 4. Choose what to measure

The generated file already declares the outputs to keep from each run. Every
file eqasim's `synthesis.output` stage writes with your eqasim configuration
(here the six CSV tables and the four spatial files of the default
`output_formats`, `[csv, gpkg]`) is archived so that a later analysis needs no
new simulation; these files have no reference, so they produce no indicator.
Remove the ones you do not need to save disk space: the spatial files grow
large at high sampling rates. Three distributions are compared with reference
data:

```yaml
outputs:
  persons: {}
  households: {}
  activities: {}
  trips: {}
  vehicle_types: {}
  vehicles: {}
  activities_spatial: {}
  homes_spatial: {}
  commutes_spatial: {}
  trips_spatial: {}
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references: [HTS]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
```

There is no `indicators` block, so eqasim-sa computes by default the total
variation distance (`tv_distance`) of every output against each of its
references. This produces three numbers per run:

| Produced name | What it compares |
| --- | --- |
| `tv_distance(age, ref=INSEE)` | synthetic ages with INSEE ages |
| `tv_distance(distance, ref=HTS)` | synthetic travel distances with the travel survey |
| `tv_distance(nbr_vehicle, ref=HTS)` | synthetic vehicle counts with the travel survey |

For Nantes/EDGT 44, the distance comparison is an asymmetric secondary
diagnostic: synthetic bins use Euclidean activity-to-activity distance, whereas
HTS bins use routed distance.

To keep only some of them, add an `indicators` block (see the
[YAML reference](yaml-reference.md#indicators)). Choosing a parameter without
choosing an indicator is not enough: you cannot
discuss "the effect of `sampling_rate`" without specifying **which output** you
mean.

### 5. Calibrate resources, plan, and run

Here, calibration measures the required runtime and disk space. It does not
calibrate the model's scientific parameters.

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

TARGET=configs/01-grid-sampling-rate.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

The commands have different roles:

| Command | Role |
| --- | --- |
| `plan` | writes the six positions to `plan.parquet` and seals them |
| `check` | statically validates the configuration, plan, environment, and available space |
| `estimate` | estimates runtime and storage from the calibration |
| `run` | executes the missing runs |
| `analyze` | computes the indicators and creates the report; there is no separate `report` command |

`check` without `--smoke` does not execute a real target run. Use
`check --smoke` only if you explicitly want to add two temporary validation
runs.

### 6. Read the results

The campaign state is stored under `campaigns/mon-analyse-sa-grid-sampling-rate/`:

- `plan.parquet` links each `runId` to its rate and seed;
- `analysis-generation.json` points to the current immutable generation under
  `analysis/generations/<generationId>/`;
- that generation's `indicators.parquet` contains the three TV values for each run;
- its `replication-dispersion.parquet` summarizes variation between seeds for each rate;
- its `output-summary.parquet` summarizes the entire design;
- its `governed-report.json` confirms `demonstration_noninferential` and
  `claimAuthorized: false`.

The following script prints the observed mean for each rate and indicator:

```bash
uv run --project "$ROOT/eqasim-sa" --locked python - <<'PY'
from collections import defaultdict
import json
from pathlib import Path
from statistics import fmean

import pyarrow.parquet as pq

root = Path("campaigns/mon-analyse-sa-grid-sampling-rate")
manifest = json.loads((root / "analysis-generation.json").read_text())
plan = {row["runId"]: row for row in pq.read_table(root / "plan.parquet").to_pylist()}
groups = defaultdict(list)

indicators = root / manifest["outputs"]["indicators"]["path"]
for row in pq.read_table(indicators).to_pylist():
    rate = plan[row["runId"]]["sampling_rate"]
    groups[(rate, row["indicator"])].append(row["value"])

for (rate, indicator), values in sorted(groups.items()):
    print(rate, indicator, f"mean={fmean(values):.4f}", f"min={min(values):.4f}", f"max={max(values):.4f}")
PY
```

Here is a **fictional illustration**, included only to show how to read the
table:

| `sampling_rate` | Mean age TV | Min-max across seeds |
| ---: | ---: | ---: |
| `0.001` | `0.086` | `0.083-0.090` |
| `0.002` | `0.082` | `0.080-0.084` |

In this fictional example, the difference between the means is `0.004`. It is
about the same size as the variation between seeds. A cautious interpretation
would be: "For the tested values and seeds, the `0.002` rate produces a slightly
lower mean age TV, but the difference is small relative to the observed
variation." More replications and a declared scientific design would be needed
to go beyond this observation.

Optionally, draw the figures of the published analysis:

```bash
"${SA[@]}" plot --meta-config "$TARGET"
```

Valid for `grid`: `output-summary` (one row per indicator: median, q05 to q95 and min to max across the plan positions, never model uncertainty), and `grid-response` (the published mean per value of each numeric parameter, one line per combination of the other parameters, with the 95% CI between replications when both bounds exist; skipped if no parameter is numeric). Both describe the observed values and seeds only. See [Figures](workflow-and-results.md#figures).

## What you can conclude

You can describe the observed differences between `0.001` and `0.002` for the
three indicators and the three declared seeds.

You cannot say that `sampling_rate` explains a global share of variance, that it
is the most important parameter, or that `0.002` is optimal. Grid explores only
the combinations listed in the file.

## Adapt this example

- To compare matching thresholds, write `matching_minimum_observations` in the `config:` section of your eqasim configuration, then replace the parameter with `matching_minimum_observations: [5, 20, 50]`.
- To combine two parameters, add the second under `parameters` and multiply the domain sizes by the number of seeds.
- To compare `matching_attributes` lists, follow the [structured-configurations tutorial](structured-configurations-tutorial.md).
- For local numeric triage, continue with [discrete OAT](what-when-how-discrete_oat.md).
- For scientific screening, use [canonical Morris](what-when-how-canonical_morris.md).

## Checklist

- The question names the parameter, its values, and every output being studied.
- Every parameter key already exists under `config:` in your eqasim configuration, and its `run:` list contains the stages that write the declared outputs.
- Seeds are in `experiment.seeds`, never under `parameters`.
- The number of positions fits the budget.
- A modified campaign has a new `campaign` name, and therefore a new folder.
- The results are described as a demonstration, not as scientific proof.

## Continue

- Previous: [Getting started](getting-started.md)
- Next: [Discrete OAT](what-when-how-discrete_oat.md)
- References: [YAML](yaml-reference.md), [parameters and methods](parameters-and-methods.md), and [workflow and results](workflow-and-results.md)
