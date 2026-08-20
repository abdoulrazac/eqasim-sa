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
| `execution` | optional | mapping |
| `calibration` | optional | mapping |
| `artifacts` | optional, default `{}` | mapping |
| `indicators` | optional, default `[]` | list |

No other root key is allowed. `campaign` must match
`^[A-Za-z0-9][A-Za-z0-9._-]*$`. `campaign_root` must not traverse any symbolic
link, must be outside `model.root`, and its first existing ancestor must be a
regular directory.

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

### Morris

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

These are exactly the five allowed keys. `trajectories` is a strictly positive
integer no greater than the number of candidate trajectories. `design_seed` is
a signed 64-bit integer. Replication seeds follow the Grid contract.

### Sobol

```yaml
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 16
  design_seed: 20260814
```

These are exactly the four allowed keys. `base_sample_size` is a power of two
greater than or equal to 2. `design_seed` is an unsigned 64-bit integer. The seed
is declared in `parameters.seed`, not in `replications`.

## `parameters`

Each entry requires exactly one `values` key:

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

Grid and Morris require at least one factor. When the plan is created or
validated, every Grid domain must be non-empty and homogeneous. Morris requires
homogeneous numeric domains with at least two distinct, strictly increasing
levels. Sobol requires at least two distinct values per factor.

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

## `analysis`

Grid and Morris require exactly:

```yaml
analysis:
  completeness_threshold: 1.0
```

The threshold is a finite YAML `float` in `[0, 1]`; `1` is invalid because it
becomes an integer. Morris requires `1.0`.

Sobol requires exactly:

```yaml
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
```

`bootstrap_resamples` is an integer in `[1, 2^63-1]`.
`confidence_level` is a finite `float` strictly between 0 and 1. Sobol also
requires completeness of `1.0`.

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

Morris and Sobol require at least one indicator. Grid accepts an empty list.
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

The following three examples are complete at the schema level. Their contextual
validation first requires the paths to exist, `model.root` to be the exact root
of a clean Git repository, `model.config` to be tracked and identical to its
`HEAD` blob, the manifest and lockfile to be regular files, the scratch directory
to exist, and factors/overrides to exist in the model YAML with the indicated
types. The referenced `00-calibration.yml` file must also exist and must not
contain a `calibration` block.

The versions rendered by `init` remain the canonical examples:
[Grid](../src/eqasim_sa/templates/01-grid.yml),
[Morris](../src/eqasim_sa/templates/02-morris.yml), and
[Sobol](../src/eqasim_sa/templates/03-sobol.yml).

### Grid

```yaml
campaign: demo-grid
campaign_root: ../campaigns/grid
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

### Morris

```yaml
campaign: demo-morris
campaign_root: ../campaigns/morris
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
analysis:
  completeness_threshold: 1.0
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

Unordered Morris domain:

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
