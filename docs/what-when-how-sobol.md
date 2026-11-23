# Demonstration Sobol: test the single-scramble Sobol workflow

**Practical example: check that the generated Sobol pipeline produces its analysis files.**

**English** | [Français](../docs-fr/what-when-how-sobol.md)

## At a glance

The `sobol` starter varies its inputs (the sampling rate and the simulator
seed), then calculates first-order and
total-order indices. However, it uses only one randomized Sobol sequence, called
a **scramble**, and bootstraps rows from that same sequence.

This design is a noninferential demonstration. It
answers only this question:

> Do the planning, execution, and analysis steps of the single-scramble Sobol workflow work?

It does not provide the uncertainty evidence needed for scientific factor
prioritization or factor fixing. For that, use
[multi-scramble Sobol](what-when-how-multiscramble_sobol.md).

## Three concepts before you begin

- The **first-order index** describes the variation associated with one factor alone.
- The **total-order index** includes that factor and its interactions with the others.
- An **interaction** means that the effect of one factor depends on the value of another.

In this method, the simulator seed is itself a factor, named `seed`. Its values
are still written in `experiment.seeds`, but they take part in the variance
decomposition instead of being replications.

## End-to-end practical example

This guide assumes that you generated `mon-analyse-sa/` and defined `$ROOT` and
the Bash array `SA` by following the
[Grid example](what-when-how-grid.md#2-generate-the-starter-project). Run the
commands from that generated project.

### 1. Understand the generated demonstration

After `eqasim-sa init`, the rendered `configs/03-sobol.yml` file asks a small
plumbing question: "Do numeric values and a finite set of seeds pass correctly
through the entire workflow?"

Its experiment part reads:

```yaml
campaign: mon-analyse-sa-sobol
experiment:
  method: sobol
  seeds: [1401, 1402]
  base_sample_size: 8
parameters:
  sampling_rate: [0.001, 0.002]
analysis:
  bootstrap_resamples: 99
```

The rest of the file declares the outputs (the population files and the three
distributions compared with reference data), the calibration file, the model,
and local execution. Omitted keys take their defaults: without `charter`
the campaign is a demonstration, the analysis uses `completeness_threshold: 1.0`
and `confidence_level: 0.95`, and the design seed is derived from the campaign
name. Only `bootstrap_resamples` is lowered to 99 to keep the demonstration
fast. `show-config` displays every value.

The source file `src/eqasim_sa/templates/03-sobol.yml` contains placeholders.
Run the rendered copy in your project, not this source file.

`sampling_rate` must exist under `config:` in your eqasim configuration; the
official `config.yml` already has it. As for every starter, the `run:` list of
that configuration must contain `analysis.synthesis.population` (see
[Getting started](getting-started.md#prepare-your-eqasim-configuration)). The
seed factor needs no model key. Never add `random_seed` or `seed` under
`parameters`.

### 2. Understand what is measured

Without an `indicators` block, the starter computes three TV indicators:

| Indicator | Comparison |
| --- | --- |
| `tv_distance(age, ref=INSEE)` | synthetic age distribution against INSEE |
| `tv_distance(distance, ref=HTS)` | distance distribution against HTS |
| `tv_distance(nbr_vehicle, ref=HTS)` | vehicle-count distribution against HTS |

For Nantes/EDGT 44, distance is an asymmetric secondary diagnostic: synthetic
bins use Euclidean activity-to-activity distance, whereas HTS bins use routed
distance.

TV ranges from `0` to `1`; a lower value indicates closer distributions.

### 3. Understand the design size

Sobol counts `D = 2` factors: `sampling_rate` and `seed`. With `N = 8`, the
design contains:

```text
N(2D+2) = 8 × (2×2+2) = 48 positions
```

Projecting each factor onto only two values creates positions that represent the
same final setting. The starter contains **48 statistical positions but 4
unique runs** (two rates by two seeds). These duplicates are expected in this small demonstration;
they do not make the design scientifically sufficient.

### 4. Calibrate, plan, and run

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

TARGET=configs/03-sobol.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

`analyze` directly creates the indicators, indices, and governed report. There
is no separate `report` command.

### 5. Read the results without overinterpreting them

The files are stored under `campaigns/mon-analyse-sa-sobol/`. For each factor and indicator,
`sobol-indices.parquet` contains:

| Column | Meaning in this demonstration |
| --- | --- |
| `first_order` | single-scramble estimate of the factor's effect alone |
| `total_order` | single-scramble estimate including interactions |
| `*_ci_low`, `*_ci_high` | interval obtained by bootstrapping rows from the single scramble |
| `rank` | descending order by total-order index |

Here is a **fictional illustration**, not a Nantes result:

| Factor | First order | Total order |
| --- | ---: | ---: |
| `sampling_rate` | `0.28` | `0.42` |
| `seed` | `0.45` | `0.61` |

You can check that the file was produced, that every factor appears, and that
the columns contain finite values. You must not turn this fictional table into a
statement such as "`sampling_rate` explains 28% with certainty."

Raw estimates may even fall outside `[0, 1]` with a finite sample. Do not hide or
truncate them: they are a diagnostic sign of estimation instability.

The central problem is uncertainty: the intervals come from resampling rows of
one scramble. They do not show how the indices would vary across several
independently randomized Sobol sequences.

The `governed-report.json` report records this as
`inferenceStatus: demonstration_noninferential` and `claimAuthorized: false`.

## Adapt the demonstration

You can test another scalar input through the pipeline, such as
`matching_minimum_observations: [5, 20, 50]`, in a **new** campaign (a new
`campaign` name), after writing that parameter in the `config:` section of
your eqasim configuration. The cost always follows `N(2D+2)`.

Add `income_assignation_method` (`[uniform, bhepop2]`) only if you have a
suitable income indicator: the starter's three indicators do not measure income
directly. Compute an income measure with an
[analysis script](analysis-scripts.md) reading `households` and read it with
a `value` indicator (see
[Your own calculations](yaml-reference.md#your-own-calculations)). Add `hts` only if
both prepared datasets exist, because this parameter changes both donors and
HTS references.

To compare categories or structured lists directly, prefer
[Grid](what-when-how-grid.md). It is easier to read and does not invite you to
interpret these indices as evidence.

## What you can conclude

You can say that the starter validated the pipeline and produced the
expected Sobol files.

You cannot make a scientific factor-prioritization or factor-fixing claim, or
present a bootstrap of one scramble as equivalent to several independent
scrambles.

## Checklist

- The campaign is a demonstration (no `charter` block, or `charter.status: demonstration`).
- `base_sample_size` is a power of two and at least 2.
- `experiment.seeds` holds at least two distinct integers.
- Positions and unique runs are distinguished.
- Any out-of-range indices remain visible.
- The result is presented as `demonstration_noninferential`.

## Continue

- Simple comparison: [Grid](what-when-how-grid.md)
- Scientific analysis: [Multi-scramble Sobol](what-when-how-multiscramble_sobol.md)
- References: [YAML](yaml-reference.md), [parameters and methods](parameters-and-methods.md), and [workflow and results](workflow-and-results.md)
