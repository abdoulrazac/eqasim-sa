# Parameters and methods

**English** | [Français](../docs-fr/parameters-and-methods.md)

## Factor domain

A factor value is exactly a signed 64-bit integer, a finite float, or a non-empty
string without a NUL character. Booleans, `null`, arrays, mappings, NaN, and
infinities are forbidden. All values in a domain have exactly the same Python
type: `[1, 2]` and `[1.0, 2.0]` are valid, while `[1, 2.0]` is not.

A name follows `^[A-Za-z_][A-Za-z0-9_]*$`, must exist under `config:` in the
eqasim YAML, and cannot be reserved.

Reserved column names: `designId`, `method`, `role`, `runId`, `seed`,
`contextHash`, `campaign`, `status`, `attempts`, `node`, `duration`,
`scratchBytes`.

Infrastructure or dedicated options forbidden as factors: `data_path`,
`git_binary`, `java_binary`, `java_memory`, `maven_binary`, `output_path`,
`output_prefix`, `processes`, `random_seed`, `run`, `working_directory`.

See the [model YAML inspection procedure](yaml-reference.md#parameters) to list
the keys and their types before choosing factors.

## Scientific factor or override

A factor modifies the design, appears in the plan, and contributes to the
`runId`. Use it only if its variation is part of the scientific question.

An override adapts the execution environment. It does not appear in the plan
and does not contribute to the `runId`. Only a few infrastructure options are
allowed. Changing `sampling_rate` through `execution.overrides` is therefore
rejected; declaring it in `parameters` is the correct approach.

The exact exclusions from the canonical configuration are `working_directory`
and, under `config`, `data_path`, `git_binary`, `java_binary`, `java_memory`,
`maven_binary`, `output_path`, and `processes`. `random_seed` is removed from
`config`, but the seed is carried separately in the `runId` envelope.
`output_prefix` is not excluded: its sealed value therefore affects `runId`,
even though it is a dedicated option that can be neither a factor nor an
override.

## Named age schemes

`eqasim-france` exposes two scalar string selectors. `matching_age_scheme`
controls age classes used by statistical matching and defaults to `matching_6`.
`analysis_age_scheme` controls the `age`, `age_purpose`, and `travel` output
classes and defaults to `analysis_7`.

| Scheme | Matching boundaries | Output classes |
| --- | --- | --- |
| `matching_6` | `[14, 29, 44, 59, 74, 1000]` | `0-14`, `15-29`, `30-44`, `45-59`, `60-74`, `75+` |
| `analysis_7` | `[10, 14, 17, 24, 49, 64, 1000]` | `0-10`, `11-14`, `15-17`, `18-24`, `25-49`, `50-64`, `65+` |

The generated Grid and Sobol plans vary `matching_age_scheme` between these two
names. The base model keeps `analysis_age_scheme: analysis_7` fixed so compared
indicators have the same output classes. Morris cannot carry this categorical
factor because it accepts numeric ordered domains only. Scheme names are
ordinary scalar factors and therefore affect `runId`; boundary lists and other
structured recodings are not patchable through eqasim-sa.

`init` reads the selected model YAML without executing it. It requires explicit
`matching_age_scheme: matching_6` and `analysis_age_scheme: analysis_7` entries.
This fixes historical matching for calibration and Morris, keeps output classes
stable, and gives generated runs explicit selector identity. Grid and Sobol still
replace `matching_age_scheme` through their factor domains.

## Nantes parameter catalogue

The following parameters are scientifically meaningful candidates. A key must
be explicitly present under `config:` in the sealed model YAML before it can be
declared as a factor.

| Parameter | Type and current value | What changes | Example domain | Methods |
| --- | --- | --- | --- | --- |
| `sampling_rate` | float, `0.001` | population size and Monte Carlo noise | `[0.001, 0.002, 0.005]` | Grid, Morris, Sobol |
| `matching_age_scheme` | string, `matching_6` | age cells used to select HTS donors | `[matching_6, analysis_7]` | Grid, Sobol |
| `matching_minimum_observations` | integer, `20` | minimum donor-cell size before an exact level is accepted | `[5, 20, 50]` | Grid, Morris, Sobol |
| `matching_minimum_age` | integer, `5` | minimum age of target persons entering statistical matching | `[0, 5, 12]` | Grid, Morris, Sobol |
| `income_assignation_method` | string, `uniform` | income synthesis stage, `uniform` or `bhepop2` | `[uniform, bhepop2]` | Grid, Sobol |
| `hts` | string, `edgt_44` | survey donors and survey reference distributions | `[edgt_44, entd]` | Grid, Sobol, if both datasets exist |
| `projection_year` | integer when enabled | projected synthesis population and projected INSEE reference | `[2020, 2030, 2040]` | conditional on projection data and scenario |

These example values demonstrate syntax; they are not automatically justified
scientific domains. In particular:

- a larger `matching_minimum_observations` forces earlier relaxation toward
  fewer matching attributes when donor cells are small;
- `matching_minimum_age` changes which target persons receive HTS donors, not
  only the age labels used for comparison;
- `income_assignation_method: bhepop2` requires valid Filosofi attribute
  distributions and can fall back to `uniform` for individual municipalities;
  the selected method is not retained in the current output, so this fallback
  must be instrumented before a strict method comparison;
- household income is already archived in the `households` artifact, but the
  starter pack defines no income scalar; register a custom income indicator on
  that artifact before interpreting this factor;
- changing `hts` changes both the donor population and HTS references, and may
  require a separate prepared data cache;
- `projection_year` requires both a compatible `projection_scenario` and the
  projection archive. Without it, `analysis.synthesis.population` compares to
  `data.census.filtered`; with it, the stage compares to
  `synthesis.population.projection.reweighted`, so INSEE follows the projected
  census.

The starter declares exactly three TV signals: age against INSEE, distance
against raw HTS, and number of vehicles against raw HTS. eqasim-france still
outputs `HTS adjust with INSEE population weights` for compatibility, but this
column is only a global rescaling of HTS. TV normalization removes that scale,
so new starters do not declare it as a second reference.

`matching_attributes` is scientifically important but is an ordered list, not a
scalar. The last attribute is relaxed first. It cannot currently be transported
as an eqasim-sa factor. `random_seed` is never declared as an ordinary model
parameter; use the method-specific seed mechanism described below.

### Concrete Grid example

This design crosses population size, age recoding, and donor-cell threshold. Two
replication seeds produce `2 × 2 × 3 × 2 = 24` positions:

```yaml
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
  matching_age_scheme:
    values: [matching_6, analysis_7]
  matching_minimum_observations:
    values: [5, 20, 50]
```

### Concrete Morris example

Morris accepts only ordered numeric factors. With three factors, two
trajectories, and two replication seeds, this example has
`2 × (3+1) × 2 = 16` positions:

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 2
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
  matching_minimum_observations:
    values: [5, 20, 50]
  matching_minimum_age:
    values: [0, 5, 12]
```

### Concrete Sobol example

For Sobol, the seed is itself a factor. Adding the donor-cell threshold to the
generated factors gives `D=4`; with `N=64`, the full Saltelli design contains
`64 × (2×4+2) = 640` positions before deduplication:

```yaml
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 64
  design_seed: 20260814
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
  matching_age_scheme:
    values: [matching_6, analysis_7]
  matching_minimum_observations:
    values: [5, 20, 50]
  seed:
    values: [1401, 1402, 1403, 1404]
```

## Replication seed or factor

With `seed_role: replication`, Grid and Morris repeat a scientific
configuration to measure its stochastic variability. The seed is in
`experiment.replications.seeds` and is not an ordinary factor column.

With `seed_role: factor`, Sobol includes the seed in the variance decomposition.
It is in `parameters.seed.values`. A seed can never be both a factor and a
replication in the same campaign.

`design_seed` does not control eqasim. It makes the selection of Morris
trajectories or the scrambled Sobol sample deterministic. It is independent of
the `random_seed` seed injected into each run.

## Grid

Grid requires at least one factor and accepts non-empty, homogeneous `int`,
`float`, or `string` domains when the plan is validated.
The plan parser technically allows duplicate levels, but the
`uniqueConfigSeedPairs` check makes them invalid if they produce the same
configuration and seed. Therefore, use unique levels.

For factors of sizes `L1...LD` and `R` seeds:

`positions = expected runs = R × Π Li`

Example: two rates, three income methods, and four seeds give
`2 × 3 × 4 = 24` positions. Configurations that are identical after resolution
may reduce the number of unique runs; the Grid `check` rejects them.

## Morris

Morris accepts only numeric factors. Each domain contains at least two levels
of the same type, strictly increasing and distinct. The order is therefore
scientifically meaningful: it defines the adjacent steps used for elementary
effects.

With `D` factors, `T` trajectories, and `R` replication seeds:

`positions = T(D+1)R`

The maximum number of candidate trajectories is:

`D! × Π 2(Li-1)`

Example: two factors with three levels, three trajectories, and two seeds give
`3 × (2+1) × 2 = 18` positions. The candidate ceiling is
`2! × 4 × 4 = 32` trajectories.

Several positions can share a `runId`, particularly when two trajectories pass
through the same point. They all remain in the plan to preserve the statistical
geometry, but the run is executed only once.

The analysis ranks by decreasing `mu_star`. `mu` retains the mean sign,
`sigma` describes the dispersion of effects, and `seed_std` aggregates
variability between seeds within trajectories.

## Sobol

Each Sobol domain contains at least two distinct, homogeneous values of type
`int`, `float`, or `string`. The order of modalities is significant. For a
quasi-random draw `u` in `[0,1)` and a domain of length `L`, the selected
modality is the one at index `floor(u × L)`. The seed is always a special integer
factor.

With `D` total factors, including the seed, and `N = base_sample_size`:

`positions = N(2D+2)`

`N` is a power of two greater than or equal to 2. `D` is between 2 and 10,600,
and `N(2D+2)` must fit in a signed 64-bit integer.

Example: `sampling_rate`, `income_assignation_method`, and `seed` give `D=3`.
With `N=64`, the plan contains `64 × 8 = 512` positions. Discrete projection can
create many duplicates: the number of unique runs can be much smaller than 512
without invalidating the Sobol plan.

Before publishing a plan, each factor must have at least one effective hybrid
contrast after discrete projection: some projected `AB_i` must differ from `A`
or the corresponding `BA_i` from `B`. A factor with no such contrast is
unidentifiable and is rejected. Duplicate positions in other rows remain valid
and continue to share a run.

The resulting indices are first-order and total-order indices. Ranking is
sorted by decreasing total-order index, then by increasing factor name in case
of a tie. Intervals are obtained through deterministic bootstrap; a constant
indicator or non-positive variance makes the analysis invalid.

## Indicators and total variation

An indicator transforms the artifacts from a run into a finite `float`. The
built-in `tv_distance` scalarizer compares two mass distributions after separate
normalization:

`TV(p,q) = 0.5 × Σ |p_i-q_i|`

A value of 0 indicates identical distributions; 1 indicates completely disjoint
supports within the provided classes. This measure alone does not establish the
causal or scientific quality of a model: it depends on the classes, reference,
and declared artifacts.

See the [glossary](glossary.md) for the distinctions between factor, override,
run, position, and scalarizer.
