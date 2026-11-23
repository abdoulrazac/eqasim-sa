# Canonical Morris: deciding which factors to retain

**Practical case: screening two settings in Nantes population matching.**

**English** | [Français](../docs-fr/what-when-how-canonical_morris.md)

## At a glance

Suppose that two parameters are still uncertain:
`matching_minimum_observations` and `matching_minimum_age`. You could include
them in a more expensive study, but first you want to measure the magnitude and
stability of their local effects before the next stage.

Canonical Morris answers the **screening** question:

> Which factors should we retain for the next study?

The concrete Nantes protocol uses a conservative rule: it retains both factors
by construction with `screening_top_k: 2`. It learns their ordering and its
stability, but it does not make a removal decision between them. Another study
with more factors or a smaller cutoff can use the same framework for selective
screening.

The result is deliberately cautious. A highly ranked factor must remain. A
low-ranked factor is not automatically irrelevant and cannot be fixed on the
basis of Morris alone. Fixing requires a total-order Sobol analysis and a
declared practical tolerance.

## Essential concepts

- A **factor** is an uncertain parameter varied by the campaign.
- An **estimand** is the precise quantity that the study aims to estimate.
- A **trajectory** is a path on which only one factor changes at each step.
- An **elementary effect** is the change in the indicator produced by that step.
- **Screening** retains factors that still require attention; it does not fix any factor.

Morris first places every factor on the same probability scale from `0` to `1`.
All factors then use the same step `Delta`, after which that step is mapped to
their declared values. The user declares `levels`; the software computes
`Delta`. Do not add a `delta` key to the YAML.

## A concrete Nantes case

The repository contains a complete protocol in
[`01-canonical-morris-pilot.yml`](../protocols/nantes-scientific/01-canonical-morris-pilot.yml).
It describes a real, practical case, but **this guide provides no empirical
Nantes result**: the interpretation values below are fictitious.

### 1. State the decision question

The protocol asks which settings must remain under study for the
Loire-Atlantique population synthesis using INSEE RP 2022 and EDGT 44.

| Charter element | Nantes case choice | Why |
| --- | --- | --- |
| Objective | `screening` | retain factors that still require attention |
| Use | `diagnostic` | understand the model, not predict a future policy |
| Estimand | mean age TV over simulator seeds | separate factor effects from the simulator's own noise |
| Primary indicator | `tv_distance(age, ref=INSEE)` | measure the difference between the age distribution and INSEE |
| Dependence | independent factors | required by this probability-space design |

Here, the seed is a **replication**. At each trajectory position, the simulator
is repeated with several seeds, and the study targets their conditional mean.

### 2. Declare the factors and their values

The protocol varies:

```yaml
parameters:
  matching_minimum_observations:
    values: [5, 20, 50]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Nantes protocol stress support around documented matching behavior and default 20
  matching_minimum_age:
    values: [0, 5, 12]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Nantes protocol stress support around documented matching behavior and default 5
```

`discrete_uniform` means that the three values of a factor have equal
probability in the study. It does not mean that they are equally probable in
reality by default: the provenance must explain why this support and weighting
answer the question being studied.

- `matching_minimum_observations` is the minimum number of observations required in a donor cell.
- `matching_minimum_age` is the minimum age used by the matching procedure.
- `sampling_rate` remains fixed at `0.001` and is covered by a separate robustness check because it also changes population size and numerical noise.

Both varied keys must already exist under `config:` in your Nantes eqasim
configuration.
Canonical Morris rejects factors that are structured lists, such as several
named `matching_attributes` lists.

### 3. Declare the observed outputs

The protocol uses:

```yaml
outputs:
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
indicators:
  - tv_distance:
      output: age
      reference: INSEE
  - tv_distance:
      outputs: [distance, nbr_vehicle]
```

The `indicators` block produces the same three indicators as the default, but
writes them explicitly, as a protocol should: `tv_distance(age, ref=INSEE)`,
`tv_distance(distance, ref=HTS)`, and `tv_distance(nbr_vehicle, ref=HTS)`. The
primary age indicator governs the screening decision. The two HTS
indicators remain secondary diagnostics. A lower TV means a distribution closer
to the reference, but Morris ranks the magnitude of the **change** in TV when
the factors change.

For Nantes/EDGT 44, the distance indicator is asymmetric: synthetic bins use
Euclidean activity-to-activity distance, whereas HTS bins use routed distance.
It therefore remains a secondary diagnostic that must be read with this
limitation.

### 4. Understand the pilot

The pilot declares:

```yaml
experiment:
  method: canonical_morris
  levels: 4
  trajectories: 24
  candidate_trajectories: 32
  design_seed: 20260820
  seeds: [45001, 45002]
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
  convergence_checkpoints: [8, 16, 24]
  screening_top_k: 2
```

The protocol writes every analysis value and the design seed explicitly, even
those that equal a default, so that the declaration can be read on its own. The
two seeds are replication base seeds.

SALib samples 32 candidate trajectories and locally optimizes a selection of 24;
only the selected occurrences are simulated. Sampling is with replacement, so
the sealed selection can contain the same unit-grid path more than once, with a
different derived simulation seed for each occurrence. With `D = 2`, `T = 24`,
and `R = 2`, the pilot contains:

```text
T(D+1)R = 24 × 3 × 2 = 144 positions
```

The pilot is not used to draw conclusions. In particular, it measures noise
between seeds and recommends how many replications will be needed in a **new**
production campaign.

### 5. Render and run the pilot

This procedure assumes that you completed installation and `init` in the
[Grid example](what-when-how-grid.md#2-generate-the-starter-project), that
`$ROOT` and the Bash array `SA` are still defined, and that the resource
calibration at `mon-analyse-sa/configs/00-calibration.yml` contains at least one
complete run.

The files under `protocols/nantes-scientific/` are declarations with machine
placeholders. They cannot be passed directly to `eqasim-sa`. From the
`openmole-eqasim` root, render the pilot with the paths on your machine:

```bash
mkdir -p rendered
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/01-canonical-morris-pilot.yml" \
  "$ROOT/rendered/canonical-morris-pilot.yml" \
  --model-config "$ROOT/eqasim-france/config_nantes_sa.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-pilot"
```

Among other checks, the renderer verifies the model's fixed values and replaces
the placeholders without changing the scientific question.

`config_nantes_sa.yml` stands for your copy of the Nantes eqasim configuration,
placed in the eqasim folder and made with
`cp config_nantes.yml config_nantes_sa.yml` (eqasim ships `config_nantes.yml`
itself; do not overwrite it). The renderer does not edit it: it must contain the
values fixed by the protocol, the two varied parameters
(`matching_minimum_observations`, `matching_minimum_age`), and
`analysis.synthesis.population` in its `run:` list (see the
[protocol README](../protocols/nantes-scientific/README.md)).

Next, plan the campaign. If you have a complete, compatible resource-calibration
campaign, provide it explicitly to `check` and `estimate`:

```bash
PILOT="$ROOT/rendered/canonical-morris-pilot.yml"
CALIBRATION="$ROOT/mon-analyse-sa/configs/00-calibration.yml"

"${SA[@]}" plan --meta-config "$PILOT"
"${SA[@]}" check --meta-config "$PILOT" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$PILOT" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$PILOT"
"${SA[@]}" analyze --meta-config "$PILOT"
```

Without a compatible calibration observation, `check` and `estimate` cannot
correctly quantify resources. In that case, follow your site's explicit
procedure before `run`; do not confuse this resource calibration with the
scientific pilot.

### 6. Move from pilot to production

Open the pilot's `replication-allocation.json`. Like every analysis output, it
lies in the current analysis generation: its path, relative to the campaign
folder `$ROOT/mon-analyse-sa/campaigns/canonical-morris-pilot/`, is the
`replicationAllocationPath` field of `analysis-generation.json` (and of the
`analyze` JSON result). Continue only if `status` is `recommended`. The `capped` and `infeasible`
statuses require you to revise the budget or target precision.

The production renderer reads this recommendation and creates another campaign:

```bash
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/02-canonical-morris-production.yml" \
  "$ROOT/rendered/canonical-morris-production.yml" \
  --model-config "$ROOT/eqasim-france/config_nantes_sa.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-production" \
  --pilot-campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-pilot"

PRODUCTION="$ROOT/rendered/canonical-morris-production.yml"
"${SA[@]}" plan --meta-config "$PRODUCTION"
"${SA[@]}" check --meta-config "$PRODUCTION" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$PRODUCTION" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$PRODUCTION"
"${SA[@]}" analyze --meta-config "$PRODUCTION"
```

If the pilot recommends `R` replications, production contains `24 × 3 × R =
72R` positions. The pilot remains unchanged: never change its phase or seeds to
turn it into production.

### 7. Read the production result

The files below are in the current analysis generation of the production
campaign, `analysis/generations/<generationId>/` (named by
`analysis-generation.json`). The main file is `canonical-morris-ranking.parquet`:

| Column | Plain-language meaning |
| --- | --- |
| `mu_star` | mean magnitude of the elementary effects |
| `mu_star_ci_low`, `mu_star_ci_high` | uncertainty obtained by resampling whole trajectories |
| `rank` and its interval | the factor's position and the stability of that position |
| `top_k_probability` | how often the factor is among the top `k` during bootstrap |
| `seed_std` | variability of elementary effects across seed replications, not raw output noise |

**Fictitious** illustration:

| Factor | Rank | `mu_star` | Rank interval | Top-2 probability |
| --- | ---: | ---: | ---: | ---: |
| `matching_minimum_observations` | `1` | `0.18` | `1-2` | `1.00` |
| `matching_minimum_age` | `2` | `0.11` | `1-2` | `1.00` |

With `screening_top_k: 2`, both factors remain in the study. The table does not
say that the first factor explains 18% of the variance: `mu_star` is a Morris
effect magnitude, not a variance share. Because this protocol contains exactly
two factors, their top-2 probability is necessarily `1.00`: this conservative
cutoff cannot remove either one. The ranks, their intervals, and reversals are
used here to check the ordering and its stability before FP compares both
factors. A different study that needs a top-1 cutoff must declare it before
`plan`, not select it after seeing the ranking.

FP means **factor prioritization**: the next Sobol stage compares the two retained
factors to decide which one should be learned more accurately first.

Next, check:

- `canonical-morris-convergence.parquet` for changes across checkpoints;
- `canonical-morris-rank-reversals.parquet` for rank reversals;
- `noise-floor.parquet` for raw noise between seeds, including `intrinsic_sd` and `mcse`;
- `convergence-evidence.parquet` for the stopping criterion;
- `governed-report.json` for the final status.

`pilot_only` prohibits any screening conclusion. `unstable` means that the
ranking has not converged. `conditional` permits only an internal result
conditional on the charter. The current contract leaves robustness checks
pending and keeps `claimAuthorized: false`: it does not produce an authorized
final claim.

Optionally, draw the figures of the published analysis:

```bash
"${SA[@]}" plot --meta-config "$PRODUCTION"
```

Valid for `canonical_morris`: `output-summary` (one row per indicator: median, q05 to q95 and min to max across the plan positions, never model uncertainty); `morris-plane` (the mu*-sigma plane with the CI of `mu_star`); `morris-convergence` (`mu_star` and its CI along the checkpoints); `morris-rank-reversals` (probability that the published order of a pair of factors is reversed, at the last checkpoint); and `morris-noise-floor` (intrinsic standard deviation and Monte Carlo standard error of the mean, as two separate series). Each needs its published table. Each carries the foot line with `claimAuthorized` (currently always `false`): none of them authorizes a final claim. See [Figures](workflow-and-results.md#figures).

## What you can conclude

After complete, converged production, you may state which factors are
**conditionally retained** for the next study within the declared setting,
supports, and indicator.

You may not say that an unretained factor is unimportant, that it can be fixed,
or that `mu_star` is a variance share. You may never draw a conclusion from the
pilot alone.

## Checklist

- The charter names the decision, estimand, and primary indicator.
- Each support and the factor-independence assumption are justified.
- `levels` is even and at least 4; `Delta` is not added to the YAML.
- Pilot and production have distinct names and roots.
- Production uses only a pilot recommendation with `recommended` status.
- Convergence criteria and robustness checks are read before any conclusion is written.

## Next steps

- Local demonstration: [Discrete OAT](what-when-how-discrete_oat.md)
- Prioritization or fixing: [Multi-scramble Sobol](what-when-how-multiscramble_sobol.md)
- Complete protocol: [Scientific Nantes](../protocols/nantes-scientific/README.md)
- References: [YAML](yaml-reference.md), [parameters and methods](parameters-and-methods.md), and [workflow and results](workflow-and-results.md)
