# YAML reference

**English** | [Français](../docs-fr/yaml-reference.md)

A meta-configuration describes an entire campaign. The project does not contain
a JSON Schema: `src/eqasim_sa/adapters/meta_config.py`, together with the
validators it calls, is the executable standard.

## Contents

- [Security and root blocks](#global-yaml-security)
- [Model and context](#model-and-context)
- [Experiments and parameters](#experiment)
- [Analysis, artifacts, and indicators](#analysis)
- [Calibration and execution](#calibration)
- [Complete examples](#complete-valid-examples)
- [Invalid examples](#invalid-examples)

## Global YAML security

The loader is `yaml.SafeLoader`, after which the document must be JSON-compatible.
YAML aliases, duplicate keys, non-scalar keys, and keys that do not resolve to
strings are rejected. Python objects, timestamps, and other non-JSON types are
therefore forbidden. Non-finite numbers are also rejected.

All relative paths in the meta-configuration are resolved from the directory
containing that YAML file, not from the current directory.

## Root blocks

| Key | Presence | Type |
| --- | --- | --- |
| `campaign` | required | string |
| `campaign_root` | required | path string |
| `model` | required | mapping |
| `context` | required | mapping |
| `experiment` | required | mapping |
| `parameters` | required | mapping |
| `analysis` | required | mapping |
| `charter` | optional; missing means legacy demonstration | mapping |
| `execution` | optional | mapping |
| `calibration` | optional | mapping |
| `artifacts` | optional, default `{}` | mapping |
| `indicators` | optional, default `[]` | list |

No other root key is allowed. `campaign` must match
`^[A-Za-z0-9][A-Za-z0-9._-]*$`. `campaign_root` must not traverse any symbolic
link, must be outside `model.root`, and its first existing ancestor must be a
regular directory.

## `charter`

The optional charter classifies what a campaign may claim. Omitting it preserves
old YAML as a legacy demonstration. An explicit demonstration has exactly this
form, with no additional key:

```yaml
charter:
  status: demonstration
```

A demonstration campaign validates configuration, execution, storage, and
analysis plumbing. It cannot support inferential or decision claims. Existing
Grid, historical `morris`, discrete OAT, and Sobol plans remain readable
demonstration provenance. Historical `morris` is compatibility-only; discrete
OAT is the current name for new adjacent-level demonstrations. Neither is
canonical Morris.

A scientific charter uses the complete strict schema below:

```yaml
charter:
  status: scientific
  decision:
    claim: Identify inputs that must remain under consideration
    use: diagnostic
    setting: Nantes population-synthesis policy assessment
    objective: screening
    estimand: Expected total-variation distance over simulation seeds
    primary_indicator: tv_distance(age, ref=INSEE)
  uncertainty:
    dependence:
      kind: independent
      rationale: The canonical Morris design assumes independent inputs
    inventory:
      parameters:
        - name: sampling_rate
          status: varied
          provenance: Design declaration
          rationale: Material input uncertainty
        - name: matching_minimum_age
          status: varied
          provenance: Design declaration
          rationale: Material input uncertainty
      data: []
      structures: []
      scenarios: []
      numerics: []
  budget:
    maximum_runs: 1000
    reserve_fraction: 0.1
  stopping:
    criteria:
      - metric: rank_stability
        threshold: 0.05
        consecutive_checks: 3
  robustness:
    checks:
      - name: Alternative dependence model
        rationale: Assess whether the FP conclusion changes under plausible dependence
  stochastic:
    target: conditional_mean
    random_source: simulator_seed
    intrinsic_variance: report
    common_random_numbers: within_trajectory
    allocation:
      phase: pilot
      variance_quantile: 0.9
      target_mcse: 0.01
      minimum_replications: 2
      maximum_replications: 20
      selected_replications: null
      provenance: null
```

`decision.claim`, `setting`, `estimand`, and `primary_indicator` are non-empty
strings. `setting` describes the decision context and `estimand` states the
quantity to be learned. `use` is `diagnostic` or `prognostic`; `objective` is
screening (`screening`), factor prioritization (`fp`), or factor fixing (`ff`).
`primary_indicator` must exactly equal a resolved indicator name. A finite
nonnegative float `practical_tolerance` is required exactly for `ff` and
forbidden otherwise. This generic charter rule is method-independent. For
`multiscramble_sobol` specifically, it is a dimensionless total-order
variance-share tolerance and must lie in `[0,1]`. A scientific configuration
requires at least one indicator.

Dependence is either `independent`, with exactly `kind` and a non-empty
`rationale`, or `declared`, adding a non-empty `groups` list. Every group has
exactly `factors`, `model`, and `provenance`; it contains at least two distinct
declared parameter names. Factors may not overlap between groups. The inventory
has exactly the five categories shown. Every item has exactly `name`, `status`,
`provenance`, and `rationale`; text is non-empty, status is `varied`, `fixed`, or
`excluded`, and names are unique per category. Varied names in the parameter
inventory must exactly equal the YAML parameter names.

`maximum_runs` and every `consecutive_checks` are positive signed 64-bit
integers. `reserve_fraction` is a finite float in `[0, 1)`. `criteria` is
non-empty; each criterion has exactly `metric`, positive finite float
`threshold`, and `consecutive_checks`, and metrics are unique.
`robustness.checks` is non-empty. Each check contains exactly a unique,
non-empty `name` and a non-empty `rationale`.

Every scientific charter has exactly one typed `stochastic` mode. The
`conditional_mean` form above is exclusive to `canonical_morris`; its constants
are exact. Quantile is a finite float in `(0,1]`, MCSE is a positive finite float
in primary-indicator units, minimum is at least 2, and maximum is not smaller.
For `phase: pilot`, selected replications and provenance are null and the
experiment provides at least the minimum number of base seeds. For
`phase: production`, selected replications is within the declared bounds,
provenance is non-empty, and its value exactly equals the experiment seed count.

`multiscramble_sobol` instead requires exactly:

```yaml
stochastic:
  target: finite_seed_factor
  random_source: simulator_seed
  intrinsic_variance: not_identified
  common_random_numbers: none
  support_interpretation: declared_finite_nuisance
```

It has no allocation. `parameters.seed` remains a required varied uncertainty
item, but it is a finite nuisance support: neither controllable importance nor
intrinsic simulator noise is identified.

The charter is structurally validated by `load_meta_config`. Current scientific
methods are `canonical_morris` for independent-input `screening` with
`conditional_mean`, and `multiscramble_sobol` for independent-input `fp` or `ff`
with `finite_seed_factor`. Historical `morris`, discrete OAT, and historical
Sobol are rejected by scientific charters.

The canonical plan metadata stores a normalized `scientificContract` object for
analyzers. Its root keys are exactly `charter` and `parameters`. The normalized
`charter` has exactly `status`, `decision`, `uncertainty`, `budget`, `stopping`,
`robustness`, and `stochastic`; unlike YAML input, `stopping` and `robustness` are the item
lists directly. Screening serializes `practical_tolerance: null`. Independent
dependence serializes `groups: []`. `inventory` always has exactly the five
categories above. `parameters` is sorted by name, and every entry contains
exactly `name`, `support`, and the matching `distribution`. Core plan validation
checks this complete shape and its equality with the design factors before
publication.

## `model` and `context`

```yaml
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
```

`model` requires exactly `config` and `root`, both strings. `root` is the exact
root of a Git repository. The repository must be clean. `config` must be a
regular file under this root, tracked by Git, and identical to the `HEAD` blob.

`context` requires exactly `data_manifest` and `lockfile`, both regular files.
Their contents are hashed with SHA-256. Loading does not validate the internal
structure of the manifest; `init` generates it and validates it when rerun.

The `contextHash` derives from four strings: the eqasim commit, the manifest's
SHA-256, the lockfile's SHA-256, and the installed version of `eqasim-sa`. Its
exact definition is:

```python
context_descriptor = {
    "eqasimCommit": commit,
    "dataManifestSha256": data_manifest_sha256,
    "lockfileSha256": lockfile_sha256,
    "eqasimSaVersion": version,
}
descriptor = json.dumps(
    context_descriptor,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode()
context_hash = hashlib.sha256(
    b"eqasim-sa:context:v1\0" + descriptor
).hexdigest()
```

The eqasim YAML loaded by `model.config` must itself be a JSON-safe mapping with
at least the following structure:

```yaml
working_directory: /chemin/cache
run: []
config:
  sampling_rate: 0.001
  random_seed: 1
```

`working_directory` is a string, `run` a list, and `config` a mapping. The same
safe loader forbids aliases, duplicate keys, non-text keys, and values that are
not JSON-compatible.

## `experiment`

### Grid

```yaml
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
```

These are exactly the three allowed keys. `seed_role` is `replication`. `seeds`
is a non-empty, duplicate-free list of signed 64-bit integers. Grid with the seed
as a factor is not supported.

### Historical `morris`

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

This is the exact old five-key schema. `trajectories` is a strictly positive
integer no greater than the adjacent-level candidate count, `design_seed` is a
signed 64-bit integer, and replication seeds follow the Grid contract. It is
accepted only with a missing or demonstration charter so old plans remain
readable as legacy demonstration provenance. Do not use it for a new campaign.

### Discrete OAT

```yaml
experiment:
  method: discrete_oat
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

This has exactly the same five-key shape and numeric validation as historical
`morris`, but has an independent method identity, plan metadata identity, and
design-ID domain. It requires a demonstration charter. Its trajectories move
between adjacent, strictly increasing numeric levels; analysis divides each
output difference by that local step normalized by the factor's full declared
range. These are local normalized effects, not canonical Morris effects.

### Canonical Morris

```yaml
experiment:
  method: canonical_morris
  seed_role: replication
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
  replications:
    seeds: [1301, 1302]
```

These are exactly the seven allowed experiment keys. Canonical Morris requires a
scientific charter with objective `screening`, independent parameter dependence,
and a declared distribution for every parameter. `levels` is an even integer at
least 4; `trajectories` is at least 2; `candidate_trajectories` is at least
`trajectories`, no greater than the finite candidate universe, and at most 500.
A deterministic distance-computation complexity budget is enforced in addition
to the 500 cap. `design_seed` and every replication base seed are signed 64-bit
integers; base seeds are non-empty and unique.

The common unit-probability step is stored exactly as the rational
`Delta = p/[2(p-1)]`, where `p = levels`. Values are mapped in lexicographic
factor order by the declared discrete-uniform generalized inverse. For support
length `L` and grid index `z`, the support index is 0 at `z = 0`, otherwise
`min((L*z - 1)//(p-1), L-1)`. A versioned SHA-256 derivation combines
`design_seed`, base seed, selected trajectory identity, and replication index.
All `D+1` points in that trajectory-replication block share the derived seed,
providing common random numbers within each elementary-effect contrast; derived
seeds must be unique between blocks.

The candidate pool and selected trajectories are deterministic. Selection uses
the pair with greatest sum of pairwise squared point distances, then greedily
adds the candidate with greatest summed distance to the selected set, with
lexicographic identity tie-breaking. The sealed algorithm is
`greedy-sum-pairwise-squared-distance`; it is not described as maximin or as a
Campolongo-optimal design.

Before publishing a canonical Morris or multi-scramble Sobol plan, structural
design positions, without run-ID deduplication, must not exceed
`floor(maximum_runs * (1 - reserve_fraction))`. The reserved fraction remains
outside the initial execution budget. Canonical plans use plan schema v2 and
seal the complete normalized scientific contract, distributions, supports,
candidate identities, selected trajectories, Delta, seed derivation, and
analysis settings. Loading and analysis reconstruct and compare that contract.

`init` does not generate a canonical Morris starter because the scientific
claim, uncertainty inventory, estimand, budget, convergence criteria, robustness
checks, supports, and provenance must be study-specific. The following is only
an illustrative schema fragment; it deliberately omits model, context,
artifacts, indicators, execution, calibration, and most inventory entries, so it
is not a ready campaign:

```yaml
charter:
  status: scientific
  decision:
    claim: <study-specific screening claim>
    use: diagnostic
    setting: <declared decision setting>
    objective: screening
    estimand: <expected indicator over simulation seeds>
    primary_indicator: <resolved indicator name>
  uncertainty:
    dependence:
      kind: independent
      rationale: <study-specific justification>
    inventory:
      parameters:
        - name: sampling_rate
          status: varied
          provenance: <source>
          rationale: <why varied>
      data: []
      structures: []
      scenarios: []
      numerics: []
  budget:
    maximum_runs: 1000
    reserve_fraction: 0.1
  stopping:
    criteria:
      - metric: <declared metric>
        threshold: 0.05
        consecutive_checks: 2
  robustness:
    checks:
      - name: <declared check>
        rationale: <why required>
  stochastic:
    target: conditional_mean
    random_source: simulator_seed
    intrinsic_variance: report
    common_random_numbers: within_trajectory
    allocation:
      phase: pilot
      variance_quantile: 0.9
      target_mcse: 0.01
      minimum_replications: 2
      maximum_replications: 20
      selected_replications: null
      provenance: null
experiment:
  method: canonical_morris
  seed_role: replication
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
  replications:
    seeds: [1301, 1302]
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: <study-specific support source>
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 1
```

### Historical Sobol

```yaml
charter:
  stochastic:
    target: finite_seed_factor
    random_source: simulator_seed
    intrinsic_variance: not_identified
    common_random_numbers: none
    support_interpretation: declared_finite_nuisance
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 16
  design_seed: 20260814
```

These are exactly the four allowed keys. `base_sample_size` is a power of two
greater than or equal to 2. `design_seed` is an unsigned 64-bit integer. The seed
is declared in `parameters.seed`, not in `replications`.

This historical method is demonstration-only and retains its single-scramble
row bootstrap. Its manifest records `inferenceStatus: legacy_noninferential`.

### Multi-scramble Sobol

Scientific FP/FF campaigns use this separate exact experiment:

```yaml
experiment:
  method: multiscramble_sobol
  seed_role: factor
  base_sample_sizes: [2, 4, 8, 16]
  scramble_seeds: [101, 202, 303, 404]
analysis:
  completeness_threshold: 1.0
  confidence_level: 0.95
```

`base_sample_sizes` is nonempty, sorted, starts at a power of two at least 2,
and each value exactly doubles its predecessor. `scramble_seeds` contains at
least four unique unsigned 64-bit integers. `design_seed` is forbidden. The
maximum planned size is the last checkpoint. The scientific charter must have
independent dependence and objective `fp` or `ff`; screening is rejected. FF
requires a dimensionless total-order variance-share `practical_tolerance` in
`[0,1]`. Every parameter, including the finite
simulator-seed support, has a declared scientific distribution.
The seed is explicitly non-actionable; its aggregate and per-scramble rows keep
diagnostic indices but have `is_actionable: false`, null rank, and null FF
tolerance. The finite support does not estimate unrestricted intrinsic noise.
The implementation rejects plans above 1,000,000 design positions or 20,000,000
Arrow table cells before QMC or table allocation; reduce the declared design
rather than bypassing these guards.

## `parameters`

For a missing or demonstration charter, each entry requires exactly one `values`
key:

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
  income_assignation_method:
    values: [uniform, bhepop2]
```

Allowed values are `int`, finite `float`, or a non-empty string without NUL.
They are homogeneous within each domain. Booleans, `null`, lists, mappings, NaN,
and infinities are rejected. The name must be a safe synpp option, an active key
in `model.config.config`, and a valid, non-reserved factor identifier.

Grid, historical `morris`, and discrete OAT require at least one factor. When the plan is created or
validated, every Grid domain must be non-empty and homogeneous. Historical
`morris` and discrete OAT require
homogeneous numeric domains with at least two distinct, strictly increasing
levels. Canonical Morris and Sobol require at least two distinct homogeneous
scalar values per factor; canonical supports may be numeric or textual because
mapping occurs through the declared distribution.

To inspect candidate keys and their actual types before writing `parameters`,
run the following from the model root:

```bash
uv run --locked python - <<'PY'
from pathlib import Path
import yaml

document = yaml.safe_load(Path("config_local_nantes.yml").read_text())
for name, value in sorted(document["config"].items()):
    print(f"{name}: {type(value).__name__} = {value!r}")
PY
```

Keep only an active, non-reserved scientific key and values of the same type as
the corresponding synpp option.

For Sobol only:

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002]
  seed:
    values: [1401, 1402]
```

`seed.values` contains at least two distinct signed 64-bit integers. The seed is
special and does not need to exist in the model configuration.

With `charter.status: scientific`, every parameter (including `seed`) instead
requires exactly `values` and `distribution`:

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Bounds declared in protocol version 2
```

For current list-backed domains, only `family: discrete_uniform` and
`transform: identity` are accepted. `provenance` is a non-empty string. The
parsed family and transform declarations are retained as typed
meta-configuration data so future samplers can extend the supported vocabulary.
A varied discrete-uniform support contains at least two distinct values;
uniformity applies to support points, never to repeated list positions.

## `analysis`

Grid, historical `morris`, and discrete OAT require exactly:

```yaml
analysis:
  completeness_threshold: 1.0
```

The threshold is a finite YAML `float` in `[0, 1]`; `1` is invalid because it
becomes an integer. Both adjacent-level trajectory methods require `1.0`.

Historical Sobol requires exactly:

```yaml
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
```

`bootstrap_resamples` is an integer in `[1, 2^63-1]`.
`confidence_level` is a finite `float` strictly between 0 and 1. Sobol also
requires completeness of `1.0`.

Multi-scramble Sobol requires exactly `completeness_threshold: 1.0` and a finite
float `confidence_level` in `(0, 1)`. Its convergence sizes come only from
`experiment.base_sample_sizes`; it has no bootstrap setting.

Canonical Morris requires exactly:

```yaml
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 2
```

These are exactly the five analysis keys. It requires at least 100 bootstrap resamples. Checkpoints are sorted, unique
integers of at least 2 and end at `trajectories`. `screening_top_k` is between 1
and the number of factors. Bootstrap resamples whole trajectories after effects
have first been averaged over replication seeds. It supplies finite-design
uncertainty evidence conditional on the sealed contract, not automatic
certification of the charter's stopping or robustness requirements.

## `artifacts`

This block maps a logical name to columns in a Parquet table:

```yaml
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
```

Each artifact requires exactly `class`, `synthetic`, and `references`.
`class` and `synthetic` are distinct, non-empty strings. `references` is a
possibly empty, duplicate-free list of non-empty strings distinct from the two
other columns. With the eqasim adapter, every artifact referenced by an
indicator must be produced by a known step: `persons`, `households`,
`activities`, `trips`, `vehicle_types`, `vehicles`, `age`, `age_purpose`,
`distance`, `license`, `nbr_vehicle`, or `travel`.

Before declaring the columns, inspect a Parquet file produced by a known run:

```bash
uv run --project ../eqasim-sa --locked python - <<'PY'
import pyarrow.parquet as pq

path = "campaigns/calibration/archives/<runId>/age.parquet"
parquet = pq.ParquetFile(path)
print(parquet.schema_arrow)
print(parquet.read().column_names)
PY
```

Replace `<runId>` with an actual identifier. The `class`, `synthetic`, and
`references` strings must exactly reproduce the displayed column names.

## `indicators`

Each item contains exactly one type: `pair`, `product`, or `custom`.
The only built-in scalarizer is `tv_distance`.

### Pair

```yaml
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
```

All three keys are required and are non-empty strings. The artifact and
reference must have been declared.

### Cartesian product

```yaml
indicators:
  - product:
      scalarizers: [tv_distance]
      artifacts: [distance, nbr_vehicle]
```

Both lists are non-empty and contain no duplicates. The product creates one
indicator for every scalarizer, artifact, and reference column declared by that
artifact.

### Custom extension

```yaml
indicators:
  - custom:
      function: mon_indicateur
      artifact: age
```

Both keys are non-empty strings. `function` must match exactly one installed
entry point in the `eqasim_sa.indicators` group. The loaded object must be
callable or provide `compute(artifacts)` and return a finite `float`.

Historical `morris`, discrete OAT, canonical Morris, and Sobol require at least
one indicator. Grid accepts an empty list.
Resolved indicator names and identities must be unique within the analysis.

### Total variation distance

`tv_distance` reads the class, synthetic, and reference columns. Both mass
columns must be numeric, finite, without null values, non-negative, and have a
positive sum. They are normalized separately, then:

`TV = 0.5 × Σ |p_i - q_i|`, with a result bounded in `[0, 1]`.

## `calibration`

```yaml
calibration:
  meta_configs: [00-calibration.yml]
```

The mapping contains exactly `meta_configs`, a non-empty list of strings. The
paths designate regular files, are deduplicated, cannot refer to the file itself,
and cannot in turn contain a `calibration` block. Additional paths can be passed
to `check` and `estimate` through the CLI.

## `execution`

```yaml
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
```

The required keys are exactly `backend`, `workers`, `scratch`, and `invocation`;
`overrides` and `slurm` are optional. The entire block is optional for `plan`
and `analyze`, but required for `run`, `check`, and `estimate`.

`backend` is `local` or `slurm`. `workers` is a strictly positive integer.
`scratch` is a non-empty string without NUL or symbolic-link traversal that must
already designate a directory. `invocation` is a non-empty list of non-empty
strings without NUL.

### `overrides`

The mapping is empty by default. Only environment options present in the sealed
YAML are allowed: `data_path`, `git_binary`, `java_binary`, `java_memory`,
`maven_binary`, and `processes`. The value must retain exactly the type from the
model YAML and be transportable as a synpp scalar; booleans are not.
`data_path` must resolve to an existing directory without traversing a symbolic
link. For SLURM, `processes` must be a strictly positive integer.

Overrides do not contribute to the `runId`: they describe infrastructure, not a
scientific factor. They are applied only at execution time.

### `slurm`

```yaml
execution:
  backend: slurm
  workers: 20
  scratch: /scratch/mon-etude
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    processes: 4
  slurm:
    timeout_min: 1440
    mem_gb: 16.0
    partition: compute
    account: equipe-transport
    qos: normal
```

`slurm` is forbidden with the local backend. All its keys are optional.
`timeout_min` defaults to 1440 and must be a positive integer. `mem_gb` is a
finite integer or float greater than or equal to `1/1024`; `null` means absent.
`partition`, `account`, and `qos` are `null` or safe identifiers following the
same pattern as `campaign`. `cpus_per_task` comes from `overrides.processes`, or
is 1.

## Complete valid examples

The following three generated examples are complete at the schema level. Their contextual
validation first requires the paths to exist, `model.root` to be the exact root
of a clean Git repository, `model.config` to be tracked and identical to its
`HEAD` blob, the manifest and lockfile to be regular files, the scratch directory
to exist, and factors/overrides to exist in the model YAML with the indicated
types. The referenced `00-calibration.yml` file must also exist and must not
contain a `calibration` block.

The versions rendered by `init` are demonstration starters:
[Grid](../src/eqasim_sa/templates/01-grid.yml),
[discrete OAT](../src/eqasim_sa/templates/02-discrete-oat.yml), and
[Sobol](../src/eqasim_sa/templates/03-sobol.yml).
There is no generated canonical Morris starter.

### Grid

```yaml
campaign: demo-grid
campaign_root: ../campaigns/grid
charter:
  status: demonstration
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
analysis:
  completeness_threshold: 1.0
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
calibration:
  meta_configs: [00-calibration.yml]
```

### Discrete OAT

```yaml
campaign: demo-discrete-oat
campaign_root: ../campaigns/discrete-oat
charter:
  status: demonstration
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
analysis:
  completeness_threshold: 1.0
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
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
calibration:
  meta_configs: [00-calibration.yml]
```

### Sobol

```yaml
campaign: demo-sobol
campaign_root: ../campaigns/sobol
charter:
  status: demonstration
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
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
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
calibration:
  meta_configs: [00-calibration.yml]
```

## Invalid examples

Required float written as an integer:

```yaml
analysis:
  completeness_threshold: 1
```

Seed used simultaneously as a factor and replication:

```yaml
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1]
parameters:
  seed:
    values: [1, 2]
```

Unordered historical-Morris/discrete-OAT domain:

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.003, 0.002]
```

Scientific or dedicated override:

```yaml
execution:
  overrides:
    sampling_rate: 0.002
    output_path: /tmp/output
```

Forbidden alias and duplicate key:

```yaml
parameters: &params {}
parameters: *params
```
