# Discrete OAT: observe local changes step by step

**Practical example: summarize the local effect of `sampling_rate` across two neighborhoods.**

**English** | [Français](../docs-fr/what-when-how-discrete_oat.md)

## At a glance

OAT means "one factor at a time." The method changes one numeric parameter while
the others remain fixed. It compares only neighboring values: for example,
`0.001` with `0.002`, then `0.002` with `0.003`.

It answers a local, aggregated question:

> Averaged across the visited neighborhoods, which numeric parameters produce the largest output changes?

This method is useful for a demonstration or an initial diagnosis. It is not a
scientific Morris design: its steps are not all the same size, and it provides
neither variance shares nor proof that a factor can be discarded.

## How it differs from Grid

Grid runs every combination. OAT instead builds paths: at each step, only one
parameter changes. With several parameters, this costs less than a large grid,
but the result depends on the neighborhoods that the paths visit.

With only one parameter, as in the example below, OAT mainly teaches you how to
read a **normalized local effect**: the change in the indicator is divided by
the relative size of the step within the declared domain.

## End-to-end practical example

This guide assumes that you have completed the project-generation steps in the
[Grid example](what-when-how-grid.md#2-generate-the-starter-project). Run the
commands from `mon-analyse-sa/`, with `$ROOT` and the Bash array `SA` defined as
shown in the [getting-started guide](getting-started.md#install-with-uv).

### 1. Ask the question

Question: "Averaged across the `0.001 ↔ 0.002` and `0.002 ↔ 0.003`
transitions selected by the design, does `sampling_rate` produce a large local
change in distance from the references?"

We observe the same three indicators as in the [Grid example](what-when-how-grid.md):
age against INSEE, distance against HTS, and number of vehicles against HTS.
For Nantes/EDGT 44, the distance indicator is an asymmetric secondary
diagnostic: synthetic bins use Euclidean activity-to-activity distance, whereas
HTS bins use routed distance.

### 2. Use the generated file

After `eqasim-sa init`, the rendered `configs/02-discrete-oat.yml` file already
contains this example:

```yaml
campaign: mon-analyse-sa-discrete_oat
experiment:
  method: discrete_oat
  seeds: [1301, 1302]
  trajectories: 2
  design_seed: 20260817
parameters:
  sampling_rate: [0.001, 0.002, 0.003]
```

The other blocks in the rendered file declare the three outputs (and so, by
default, the three indicators), the calibration file, the model and its data,
and local execution. Without a `charter` block, the campaign is a
demonstration, which discrete OAT requires. `design_seed` is written explicitly
so that the starter selects the same trajectories whatever the project name. The source file
`src/eqasim_sa/templates/02-discrete-oat.yml` contains placeholders; run the copy
created in your project by `init`.

The values must be numeric, distinct, and strictly increasing. The seeds are
replications declared in `experiment.seeds`; never declare `random_seed` or
`seed` as an analysis parameter.

### 3. Understand the design size

The file declares:

- `D = 1` parameter;
- `T = 2` trajectories;
- `R = 2` replication seeds.

The design therefore contains `T(D+1)R = 2 × 2 × 2 = 8` positions. Some
positions may represent exactly the same setting and seed. The starter therefore
produces **8 statistical positions but 6 unique runs**. The simulator does not
execute the same `runId` twice. If you remove `design_seed`, its default is
derived from the campaign name and the number of unique runs may change.

### 4. Understand the compared steps

The total range is `0.003 - 0.001 = 0.002`:

| Change | Step size | Fraction of the range |
| --- | ---: | ---: |
| `0.001 → 0.002` | `0.001` | `0.5` |
| `0.002 → 0.003` | `0.001` | `0.5` |

For each step, seed, and indicator, the method calculates the output change
divided by the signed step fraction. It then aggregates these effects into **one
row per factor and indicator**. The final file therefore does not contain one row
for `0.001 → 0.002` and another for `0.002 → 0.003`. This normalization makes
the effects comparable within the declared domain, but it does not turn them
into variance shares.

If you use `[0.001, 0.002, 0.005]` instead, the fractions become `0.25` and
`0.75`. The two effects were then observed over different distances and must
still be interpreted as local changes.

### 5. Plan, check, and run

From the generated project, start with resource calibration:

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

TARGET=configs/02-discrete-oat.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

`check` is static unless you use `--smoke`. `analyze` computes the indicators
and effects; it also creates the governed report.

### 6. Read the results

The results are stored under `campaigns/mon-analyse-sa-discrete_oat/`. The main file is
`discrete-oat-effects.parquet`. It contains one row per factor and indicator,
including:

| Column | Simple interpretation |
| --- | --- |
| `mu` | mean local effect with its sign; positive means that the indicator increases as the factor increases |
| `mu_star` | mean effect magnitude without canceling opposing signs |
| `sigma` | standard deviation of all `T × R` elementary effects; it combines variation across trajectories and seeds |
| `seed_std` | square root of the mean across-seed variance, computed separately within each trajectory |
| `rank` | descriptive order by `mu_star` |

Suppose, in a **fictional illustration**, that the row for the age indicator has
`mu_star = 0.012` and `seed_std = 0.010`. The rate's mean local effect is then
only slightly greater than the variation between seeds. This suggests keeping
the parameter under consideration; it does not demonstrate a global effect.

If `mu` is close to zero but `mu_star` is large, the observed steps produced
effects with opposing signs that cancel in `mu`. If `sigma` is large, the
dispersion may come from trajectories, neighborhoods, **or seeds**; compare it
with `seed_std` before attributing it to local nonlinearity.

The other files help you verify this interpretation:

- `indicators.parquet` contains each indicator value for every run;
- `replication-dispersion.parquet` shows dispersion across seeds;
- `governed-report.json` contains `demonstration_noninferential` and `claimAuthorized: false`.

## Extend the example to several parameters

In a new campaign, you can compare, for example:

```yaml
parameters:
  sampling_rate: [0.001, 0.002, 0.005]
  matching_minimum_observations: [5, 20, 50]
  matching_minimum_age: [0, 5, 12]
```

With two trajectories and two seeds, this gives
`2 × (3 + 1) × 2 = 16` positions. Every key must already exist under `config:`
in the sealed model. Discrete OAT rejects categories and structured lists; use
[Grid](what-when-how-grid.md) for those.

## What you can conclude

You can compare the factors' **aggregated local effects** across the neighborhoods
selected in the demonstration. The aggregate file cannot rank the two
`sampling_rate` transitions separately.

You cannot say that a small local effect proves a factor is unimportant, because
OAT does not cover every combination or every interaction. You cannot make
claims about variance shares, factor prioritization, or factor fixing.

To decide scientifically which factors must remain under study, use
[canonical Morris](what-when-how-canonical_morris.md).

## Checklist

- The question specifies the outputs and numeric neighborhoods being studied.
- Every domain contains at least two strictly increasing values.
- The parameters are numeric; no structured configuration is used.
- The number of positions is calculated before `plan`.
- A modified configuration uses a new `campaign` name, and therefore a new folder.
- The result is presented as descriptive local triage, not scientific screening.

## Continue

- Previous: [Grid](what-when-how-grid.md)
- Next: [Canonical Morris](what-when-how-canonical_morris.md)
- References: [YAML](yaml-reference.md), [parameters and methods](parameters-and-methods.md), and [workflow and results](workflow-and-results.md)
