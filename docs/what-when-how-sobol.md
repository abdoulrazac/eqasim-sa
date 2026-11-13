# Demonstration Sobol: test the single-scramble Sobol workflow

**Practical example: check that the generated Sobol pipeline produces its analysis files.**

**English** | [Français](../docs-fr/what-when-how-sobol.md)

## At a glance

The `sobol` starter varies several inputs, then calculates first-order and
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

In this method, the simulator seed is itself a factor. It therefore does not
appear in `experiment.replications.seeds`.

## End-to-end practical example

This guide assumes that you generated `mon-analyse-sa/` and defined `$ROOT` and
the Bash array `SA` by following the
[Grid example](what-when-how-grid.md#2-generate-the-starter-project). Run the
commands from that generated project.

### 1. Understand the generated demonstration

After `eqasim-sa init`, the rendered `configs/03-sobol.yml` file asks a small
plumbing question: "Do numeric values, categories, and a finite set of seeds pass
correctly through the entire workflow?"

It contains exactly:

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
  sampling_rate:
    values: [0.001, 0.002]
  matching_age_scheme:
    values: [matching_6, analysis_7]
  seed:
    values: [1401, 1402]
```

The source file `src/eqasim_sa/templates/03-sobol.yml` contains placeholders.
Run the rendered copy in your project, not this source file.

The two ordinary parameters must already exist under `config:` in the sealed
model. `seed` is the only special exception: it must not exist under `config:`.
Never add `random_seed` as a factor.

### 2. Understand what is measured

The starter declares three TV indicators:

| Indicator | Comparison |
| --- | --- |
| `tv_distance(age, ref=INSEE)` | synthetic age distribution against INSEE |
| `tv_distance(distance, ref=HTS)` | distance distribution against HTS |
| `tv_distance(nbr_vehicle, ref=HTS)` | vehicle-count distribution against HTS |

For Nantes/EDGT 44, distance is an asymmetric secondary diagnostic: synthetic
bins use Euclidean activity-to-activity distance, whereas HTS bins use routed
distance.

TV ranges from `0` to `1`; a lower value indicates closer distributions. The
`matching_age_scheme` parameter affects matching, while `analysis_age_scheme`
remains fixed at `analysis_7` in the model so that the output classes stay
comparable.

### 3. Understand the design size

Sobol counts `D = 3` factors: `sampling_rate`, `matching_age_scheme`, and `seed`.
With `N = 4`, the design contains:

```text
N(2D+2) = 4 × (2×3+2) = 32 positions
```

Projecting each factor onto only two values creates positions that represent the
same final setting. The starter normally contains **32 statistical positions but
8 unique runs**. These duplicates are expected in this small demonstration;
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

The files are stored under `campaigns/sobol/`. For each factor and indicator,
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
| `matching_age_scheme` | `0.12` | `0.31` |
| `seed` | `0.25` | `0.37` |

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
`matching_minimum_observations: {values: [5, 20, 50]}`, in a **new** campaign
with a new root. The cost always follows `N(2D+2)`.

Add `income_assignation_method` only if you have a suitable income indicator:
the starter's three indicators do not measure income directly. Add `hts` only if
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

- The campaign has `charter.status: demonstration`.
- `base_sample_size` is a power of two and at least 2.
- `seed` is an integer factor with at least two distinct values.
- Positions and unique runs are distinguished.
- Any out-of-range indices remain visible.
- The result is presented as `demonstration_noninferential`.

## Continue

- Simple comparison: [Grid](what-when-how-grid.md)
- Scientific analysis: [Multi-scramble Sobol](what-when-how-multiscramble_sobol.md)
- References: [YAML](yaml-reference.md), [parameters and methods](parameters-and-methods.md), and [workflow and results](workflow-and-results.md)
