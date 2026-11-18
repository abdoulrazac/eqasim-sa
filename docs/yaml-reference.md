# YAML reference

**English** | [Français](../docs-fr/yaml-reference.md)

A meta-configuration is the single YAML file that describes one experiment (a
campaign): which model parameters vary, which method explores them, and which
model outputs are compared with which reference data. One experiment is one
file; several experiments are several files.

You write only what matters for your study. Everything that has a single
sensible value, or that `init` already knows, has a default. The command
`eqasim-sa show-config FILE` prints the complete file with every default filled
in (see [Defaults and `show-config`](#defaults-and-show-config)).

The project contains no JSON Schema: `src/eqasim_sa/adapters/meta_config.py`,
together with the validators it calls, is the executable standard, and
`src/eqasim_sa/adapters/meta_config_defaults.py` holds every default value.

## Contents

- [A first file](#a-first-file)
- [Defaults and `show-config`](#defaults-and-show-config)
- [General rules](#general-rules)
- [Root keys](#root-keys)
- [`model`](#model)
- [`experiment`](#experiment)
- [`parameters`](#parameters)
- [`analysis`](#analysis)
- [`outputs`](#outputs)
- [`indicators`](#indicators)
- [`execution`](#execution)
- [`calibration`](#calibration)
- [`charter`](#charter)
- [Complete valid examples](#complete-valid-examples)
- [Invalid examples](#invalid-examples)

## A first file

This is the Grid starter that `init` writes in `configs/01-grid.yml`, here for a
project called `mon-analyse-sa`:

```yaml
# Two rates by two seeds: four positions and four unique runs.
campaign: mon-analyse-sa-grid
experiment:
  method: grid
  seeds: [1201, 1202]
parameters:
  sampling_rate: [0.001, 0.002]
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
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
calibration:
  meta_configs: [00-calibration.yml]
# --- Rempli par eqasim-sa init : à modifier seulement si la machine change ---
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

The generated file also carries a few comment lines (omitted here) that recall
the prerequisite on your eqasim configuration and show how to add a second
parameter.

Read it from top to bottom:

- `campaign` names the experiment. Its results go to
  `../campaigns/mon-analyse-sa-grid`.
- `experiment` chooses the method (`grid` tries every combination) and the
  simulation seeds. Each combination is run once per seed.
- `parameters` lists, for each eqasim parameter you vary, the values to try.
  The starter varies only `sampling_rate`, which the official eqasim
  configuration already contains.
- `outputs` lists the files of each run that eqasim-sa keeps. A known eqasim
  output such as `persons` needs nothing more (`{}`); `activities_spatial` is
  not a known name, so its file is written explicitly. For the three
  distributions `age`, `distance` and `nbr_vehicle`, the block also says which
  column holds the classes (`category`), which one holds the simulated
  proportions (`simulated`), and which reference columns to compare them with
  (`references`). Without an `indicators` block, the total variation distance
  is computed for every output against each of its references.
- `calibration` points to the resource-calibration file used by `check` and
  `estimate`.
- The part under the comment line (French for "filled in by eqasim-sa init:
  change only if the machine changes") holds the paths of the eqasim folder,
  its configuration file, the data and the scratch directory on this machine.
  `config: config.yml` is read from the eqasim folder (`model.root`).

Before you plan anything, open your copy of the eqasim configuration and check
that its `run:` list contains `analysis.synthesis.population`: the `age`,
`distance` and `nbr_vehicle` files are written by that eqasim stage, and the
official `config.yml` only lists `synthesis.output`. See
[What eqasim runs](#what-eqasim-runs).

## Defaults and `show-config`

When a key is omitted, eqasim-sa applies the following default values.

| Key | Default |
| --- | --- |
| `campaign_root` | `../campaigns/<campaign>` |
| `model.lockfile` | `<model.root>/uv.lock` |
| `experiment.design_seed` (discrete OAT, canonical Morris, Sobol) | derived from the campaign name (see below) |
| `parameters.NAME: [a, b]` | the same as `parameters.NAME: {values: [a, b]}` |
| `analysis` | an empty block, completed as below |
| `analysis.completeness_threshold` | `1.0` (all methods) |
| `analysis.bootstrap_resamples` | `999` (Sobol, canonical Morris) |
| `analysis.confidence_level` | `0.95` (Sobol, canonical Morris, multi-scramble Sobol) |
| `outputs.NAME.file`, `outputs.NAME.separator` (known output) | the eqasim file and separator listed in [Known outputs](#known-outputs-and-their-files) |
| `outputs.NAME.separator` (other output) | `,` |
| `outputs.NAME.references` | an empty list |
| `indicators` | one total variation distance for every output that has at least one reference |
| `execution.backend` | `local` (only when an `execution` block is present) |
| `execution.workers` | `1` (only when an `execution` block is present) |
| `execution.invocation` | `[uv, run, --locked, python, -m, synpp]` (only when an `execution` block is present) |

Paths are relative to the directory of the YAML file, so the default campaign
root sits next to the `configs/` directory. Default paths use `/`, which is
valid on Windows, macOS, and Linux.

The default `design_seed` is computed from the campaign name: the first eight
bytes of `SHA-256(b"eqasim-sa:design-seed:v1\0" + UTF8(campaign))`, read as a
big-endian integer and shifted right by one bit. It is therefore always in
`[0, 2^63)`, which suits every method. Renaming a campaign changes its default
design seed, and so its design; write `design_seed` explicitly if you want to
keep the design under another name.

Defaults never change silently under a running campaign. The resolved file
depends only on the bytes of your YAML file (sealed as `metaConfigSha256`) and
on the eqasim-sa version (part of the `contextHash`, which already refuses to
resume a campaign with another version). Nothing extra is written to disk.

To see the complete file that eqasim-sa actually uses, run:

```bash
"${SA[@]}" show-config configs/01-grid.yml
```

It validates the file the same way `plan` does (eqasim folder and
configuration found, factors present in the eqasim configuration), then prints the complete YAML on
standard output with every default applied. Paths are printed as you wrote
them, so the output is itself a valid meta-configuration. If the file is
invalid, the error message goes to standard error and the exit code is 1;
unlike the other subcommands, `show-config` prints no JSON envelope. For the
starter above, the output is shown in
[Complete valid examples](#the-same-grid-with-every-default-written).

## General rules

The loader is `yaml.SafeLoader`, after which the document must be
JSON-compatible. YAML aliases, duplicate keys, non-scalar keys, and keys that do
not resolve to strings are rejected. Python objects, timestamps, and other
non-JSON types are therefore forbidden. Non-finite numbers are also rejected.

All relative paths in the meta-configuration are resolved from the directory
containing that YAML file, not from the current directory.

Every block accepts only its own keys. A misspelled or unknown key is an error,
and the message lists the keys allowed at that level.

## Root keys

| Key | Presence | Content |
| --- | --- | --- |
| `campaign` | required | name of the experiment |
| `campaign_root` | optional | folder of the campaign state and results |
| `model` | required | the eqasim model and its sealed inputs |
| `experiment` | required | method and seeds |
| `parameters` | required | varied eqasim parameters and their values |
| `analysis` | optional | analysis settings |
| `outputs` | required | files kept from each run, and the columns indicators read in them |
| `indicators` | optional | quantities computed from the outputs |
| `execution` | optional for `plan` and `analyze`; required for `run`, `check`, and `estimate` | how runs are executed on this machine |
| `calibration` | optional | resource-calibration files |
| `charter` | optional; missing means demonstration | what the campaign may claim |

No other root key is allowed. `campaign` must match
`^[A-Za-z0-9][A-Za-z0-9._-]*$` and have at most 64 characters; Windows reserved
names and case-only differences are refused on every system. `campaign_root`
must not traverse any symbolic link, must be outside `model.root`, and its first
existing ancestor must be a regular directory. Use a new `campaign` for every
changed design: with the default `campaign_root`, it also gives the campaign a
new folder.

## `model`

```yaml
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
```

`root`, `config`, and `data_manifest` are required; `lockfile` is optional and
defaults to `<root>/uv.lock`. All four are path strings.

`root` is the eqasim folder: a clone, a copy, or an unpacked archive all work
the same way. Git is not required, and eqasim-sa never calls it. `root`
must be an existing directory that is not a symbolic link.

`config` is the eqasim configuration file of your study. A relative path is
read from `root`, exactly as when you run eqasim yourself from its folder:
`config: config.yml` designates the official file, and
`config: my_config.yml` a copy you made next to it (for example with
`cp config.yml my_config.yml`, or `cp config_nantes.yml config_nantes_sa.yml`
for Nantes, since eqasim ships `config_nantes.yml` itself). An absolute path is
also accepted, so the file may live outside the eqasim folder. It must be a
regular file; it does not need to be under version control. Its content is
part of the campaign identity.

`data_manifest` and `lockfile` are regular files whose contents are hashed with
SHA-256. Loading does not validate the internal structure of the manifest;
`init` generates it and validates it when rerun.

### The eqasim code fingerprint

eqasim-sa identifies the eqasim code by a fingerprint of its content: a
SHA-256 over every `*.py` and `*.java` file and every `pom.xml`,
`pyproject.toml` and `uv.lock` under `root`.
It leaves out:

- folders whose name starts with a dot (for example `.venv`) and `__pycache__`
  folders;
- Python virtual environments and conda environments (any folder containing
  `pyvenv.cfg` or `conda-meta`), so installing packages never changes it;
- the eqasim `working_directory` (cache) and `config.output_path` folders of the
  configuration, when they lie under `root`;
- symbolic links and Windows junctions, which are neither followed nor hashed.

File contents are hashed with line endings normalized to LF, and paths are
sorted, written with `/` and normalized to Unicode NFC, so the same code gives
the same fingerprint on Windows, macOS, and Linux. Any change you make to the
eqasim code changes the fingerprint and therefore the campaign identity. During a campaign, every run checks the fingerprint again
and refuses to start if the code has changed.

The `contextHash` derives from four strings: the eqasim code fingerprint, the
manifest's SHA-256, the lockfile's SHA-256, and the installed version of
`eqasim-sa`. Its exact definition is:

```python
context_descriptor = {
    "eqasimSourceSha256": eqasim_source_sha256,
    "dataManifestSha256": data_manifest_sha256,
    "lockfileSha256": lockfile_sha256,
    "eqasimSaVersion": version,
}
descriptor = json.dumps(
    context_descriptor,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    allow_nan=False,
).encode("ascii")
context_hash = hashlib.sha256(
    b"eqasim-sa:context:v3\0" + descriptor
).hexdigest()
```

### The eqasim configuration

The eqasim YAML loaded by `model.config` must itself be a JSON-safe mapping with
at least the following structure:

```yaml
working_directory: /path/to/cache
run:
  - synthesis.output
  - analysis.synthesis.population
config:
  sampling_rate: 0.001
  random_seed: 1
```

`working_directory` is a string, `run` a list, and `config` a mapping. The same
safe loader forbids aliases, duplicate keys, non-text keys, and values that are
not JSON-compatible.

### What eqasim runs

eqasim-sa runs the `run:` list of your eqasim configuration as it is: it does
not add, remove, or reorder stages. Each run only changes the varied
parameters, the seed (`random_seed`), the cache folder (`working_directory`)
and the output folder (`output_path`), which point to the run's own scratch
space.

The `run:` list therefore decides which files exist at the end of a run. The
official `config.yml` lists only `synthesis.output`, which writes the
population files (`persons`, `households`, `trips`, `activities`, ...). The
distributions compared with census and survey data (`age`, `distance`,
`nbr_vehicle`, ...) are written by the eqasim stage
`analysis.synthesis.population`; add it to `run:` in your copy of the
configuration:

```yaml
run:
  - synthesis.output
  - analysis.synthesis.population
```

When a known output keeps its default file (see [`outputs`](#outputs)) and the
stage that writes it is missing from `run:`, loading stops with a message such
as:

> output 'age' is written by eqasim stage 'analysis.synthesis.population',
> which is not in run: of /path/to/eqasim-france/config.yml; add it to run: in
> your eqasim config

The message gives the full path of the configuration that was read.

## `experiment`

`method` chooses the design and `seeds` lists the simulation seeds. The other
keys depend on the method:

| Method | Allowed keys | Charter |
| --- | --- | --- |
| `grid` | `method`, `seeds` | demonstration or scientific |
| `discrete_oat` | `method`, `seeds`, `trajectories`, `design_seed` | demonstration |
| `canonical_morris` | `method`, `seeds`, `levels`, `trajectories`, `candidate_trajectories`, `design_seed` | scientific |
| `sobol` | `method`, `seeds`, `base_sample_size`, `design_seed` | demonstration |
| `multiscramble_sobol` | `method`, `seeds`, `base_sample_sizes`, `scramble_seeds`, `seeds_provenance` | scientific |

`design_seed` is optional; its default is derived from the campaign name (see
[Defaults](#defaults-and-show-config)). Every other key in the table is
required.

### Seeds

`experiment.seeds` is the only place where simulation seeds are written. It is a
non-empty, duplicate-free list of signed 64-bit integers. Its meaning depends on
the method:

- For Grid, discrete OAT, and canonical Morris, the seeds are **replications**:
  every design point is run once per seed, and the analysis averages over them
  or reports their dispersion.
- For Sobol and multi-scramble Sobol, the seeds form the support of an extra
  **factor**, named `seed`, which takes part in the variance decomposition. At
  least two seeds are then required. Charter inventories and indicator results
  call this factor `seed`.

Declaring a parameter named `seed` under `parameters` is refused: seeds are
always written in `experiment.seeds`.

### Grid

```yaml
experiment:
  method: grid
  seeds: [1201, 1202]
```

Grid runs every combination of the declared parameter values once per seed.

### Discrete OAT

```yaml
experiment:
  method: discrete_oat
  seeds: [1301, 1302]
  trajectories: 8
```

`trajectories` is a strictly positive integer no greater than the
adjacent-level candidate count. `design_seed`, when written, is a signed 64-bit
integer. It requires a demonstration charter. Its trajectories move between
adjacent, strictly increasing numeric levels; analysis divides each output
difference by that local step normalized by the factor's full declared range.
These are local normalized effects, not canonical Morris effects.

### Canonical Morris

```yaml
experiment:
  method: canonical_morris
  seeds: [1301, 1302]
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
```

Canonical Morris requires a scientific charter with objective `screening`,
independent parameter dependence, and a declared distribution for every
parameter. `levels` is an even integer at least 4; `trajectories` is at least 2;
`candidate_trajectories` is strictly greater than `trajectories`, no greater
than the finite candidate universe, and at most 500. A deterministic
distance-computation complexity budget is enforced in addition to the 500 cap.
`design_seed` and every seed are signed 64-bit integers.

The common unit-probability step is stored exactly as the rational
`Delta = p/[2(p-1)]`, where `p = levels`. Values are mapped in lexicographic
factor order by the declared discrete-uniform generalized inverse. For support
length `L` and grid index `z`, the support index is 0 at `z = 0`, otherwise
`min((L*z - 1)//(p-1), L-1)`. A versioned SHA-256 derivation combines
`design_seed`, base seed, selected trajectory identity, and replication index.
All `D+1` points in that trajectory-replication block share the derived seed,
providing common random numbers within each elementary-effect contrast; derived
seeds must be unique between blocks.

The candidate sample and selected trajectories are deterministic for the sealed
seed. SALib 1.5.2 performs the public Morris call with local optimization and
`optimal_trajectories: trajectories`. Candidate trajectories are sampled with
replacement, so a selected matrix may occur more than once on a finite grid.
Each occurrence has a distinct selected index and derived simulation seed; all
selected matrices and effective SALib options are sealed in the v3 design.

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
an illustrative fragment; it deliberately omits `model`, `outputs`, `execution`,
`calibration`, and most inventory entries, so it is not a ready campaign. The
[Nantes scientific protocol](../protocols/nantes-scientific/README.md) contains
complete scientific files.

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
        - name: matching_minimum_age
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
  seeds: [1301, 1302]
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: <study-specific support source>
  matching_minimum_age:
    values: [0, 5, 12]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: <study-specific support source>
analysis:
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 1
```

### Demonstration Sobol

```yaml
experiment:
  method: sobol
  seeds: [1401, 1402]
  base_sample_size: 16
```

`base_sample_size` is a power of two greater than or equal to 2. `design_seed`,
when written, is an unsigned 64-bit integer. The seeds become the `seed` factor
and need at least two values. It requires a demonstration charter.

This method is demonstration-only and retains its single-scramble row
bootstrap. Its manifest records `inferenceStatus: demonstration_noninferential`.

### Multi-scramble Sobol

Scientific factor-prioritization (FP) and factor-fixing (FF) campaigns use this
method:

```yaml
experiment:
  method: multiscramble_sobol
  seeds: [44011, 44012, 44013, 44014]
  seeds_provenance: Declared finite simulator-seed nuisance support
  base_sample_sizes: [2, 4, 8, 16]
  scramble_seeds: [101, 202, 303, 404]
```

`base_sample_sizes` is non-empty, sorted, starts at a power of two at least 2,
and each value exactly doubles its predecessor. `scramble_seeds` contains at
least four unique unsigned 64-bit integers. `design_seed` is not allowed. The
maximum planned size is the last checkpoint.

The seeds form the finite support of the `seed` factor. Its scientific
distribution is implicitly `discrete_uniform` with `transform: identity`, and
`seeds_provenance`, a required non-empty string, states where that support
comes from. Every declared parameter also needs its own distribution.

The scientific charter must have independent dependence and objective `fp` or
`ff`; screening is rejected. FF requires a dimensionless total-order
variance-share `practical_tolerance` in `[0,1]`. The seed is explicitly
non-actionable; its aggregate and per-scramble rows keep diagnostic indices but
have `is_actionable: false`, null rank, and null FF tolerance. The finite
support does not estimate unrestricted intrinsic noise. The implementation
rejects plans above 1,000,000 design positions or 20,000,000 Arrow table cells
before QMC or table allocation; reduce the declared design rather than
bypassing these guards.

## `parameters`

Each entry names an eqasim parameter and lists the values to try. The short form
is a plain list:

```yaml
parameters:
  sampling_rate: [0.001, 0.002, 0.003]
  income_assignation_method: [uniform, bhepop2]
```

It is exactly the same as the long form with a single `values` key, which is
also accepted:

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
```

Allowed values are `int`, finite `float`, or a non-empty string without NUL.
They are homogeneous within each domain. Booleans, `null`, lists, mappings, NaN,
and infinities are rejected. The name must be a safe synpp option, an active key
in `model.config.config`, and a valid, non-reserved factor identifier. The name
`seed` is refused: write seeds in [`experiment.seeds`](#seeds).

"Active key" means that the parameter is written in the `config:` section of
your eqasim configuration. The official `config.yml` sets only a few of them
(such as `sampling_rate`); to vary another eqasim parameter, for example
`matching_minimum_age`, first write it in `config:` with its usual value, then
list it under `parameters`.

Grid and discrete OAT require at least one factor. When the plan is created or
validated, every Grid domain must be non-empty and homogeneous. Discrete OAT
requires homogeneous numeric domains with at least two distinct, strictly
increasing levels. Canonical Morris and Sobol require at least two distinct
homogeneous scalar values per factor; canonical supports may be numeric or
textual because mapping occurs through the declared distribution.

To inspect candidate keys and their actual types before writing `parameters`,
run the following from the eqasim folder (replace `config.yml` with your
configuration file):

```bash
uv run --locked python - <<'PY'
from pathlib import Path
import yaml

document = yaml.safe_load(Path("config.yml").read_text())
for name, value in sorted(document["config"].items()):
    print(f"{name}: {type(value).__name__} = {value!r}")
PY
```

Keep only an active, non-reserved scientific key and values of the same type as
the corresponding synpp option.

### Under a scientific charter

With `charter.status: scientific`, the short form is refused: every parameter
requires the long form with exactly `values` and `distribution`:

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
uniformity applies to support points, never to repeated list positions. The seed
factor of multi-scramble Sobol gets its distribution from
`experiment.seeds_provenance` instead.

### Named configurations

An ordered-list model key may instead use named configurations. This is generic;
`matching_attributes` is only a representative example:

```yaml
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
```

The model key must exist and contain a list of strings. Every label is a unique,
non-empty string without NUL. Every payload is a non-empty ordered list of
unique, non-empty strings without NUL, and two labels may not name the same
payload. Payload order is preserved.

Grid is the recommended first design. Demonstration Sobol is accepted.
Multi-scramble Sobol is accepted only when the labels have a defensible
scientific distribution; a scientific declaration adds the same exact
`distribution` sibling used by scalar supports:

```yaml
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Protocol-defined matching configurations
```

The label list is the factor support used by the design and scientific contract.
The plan column and `RunSpec.parameters` retain labels; the resolved payload is
hashed into `runId`. Discrete OAT and canonical Morris reject structured factors
because arbitrary nominal label order cannot define their steps. During
execution, a changed declared list is written to a per-attempt safe YAML
snapshot and omitted from the synpp command-line options; undeclared changed
lists or mappings remain invalid.

## `analysis`

The block is optional. Each method accepts a fixed set of keys; any other key is
refused, including a key that belongs to another method.

| Method | Allowed keys | Defaults |
| --- | --- | --- |
| `grid`, `discrete_oat` | `completeness_threshold` | `completeness_threshold: 1.0` |
| `sobol` | `completeness_threshold`, `bootstrap_resamples`, `confidence_level` | `1.0`, `999`, `0.95` |
| `multiscramble_sobol` | `completeness_threshold`, `confidence_level` | `1.0`, `0.95` |
| `canonical_morris` | `completeness_threshold`, `bootstrap_resamples`, `confidence_level`, `convergence_checkpoints`, `screening_top_k` | `1.0`, `999`, `0.95`; the last two have no default |

`completeness_threshold` is the fraction of expected runs that must be complete
before the analysis is published. It is a finite number in `[0, 1]`; `1` and
`0` may be written as integers. Discrete OAT, canonical Morris, Sobol, and
multi-scramble Sobol require `1.0`, so only Grid can lower it.

`bootstrap_resamples` is an integer in `[1, 2^63-1]`; canonical Morris requires
at least 100. `confidence_level` is a finite float strictly between 0 and 1.
Multi-scramble Sobol has no bootstrap setting: its convergence sizes come only
from `experiment.base_sample_sizes`.

Canonical Morris always needs an `analysis` block, because two of its keys have
no default:

```yaml
analysis:
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 2
```

Checkpoints are sorted, unique integers of at least 2 and end at
`trajectories`. `screening_top_k` is between 1 and the number of factors.
Bootstrap resamples whole trajectories after effects have first been averaged
over replication seeds. It supplies finite-design uncertainty evidence
conditional on the sealed contract, not automatic certification of the
charter's stopping or robustness requirements.

## `outputs`

`outputs` lists the files of each run that eqasim-sa keeps, and the columns
that indicators may read in them. At least one output is required:

```yaml
outputs:
  persons: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
```

Each output accepts five keys, all optional for a known output:

| Key | Content |
| --- | --- |
| `file` | path of the file, relative to the eqasim output folder (`output_path`) |
| `separator` | one-character column separator of a CSV file |
| `category` | the column holding the classes (for example the age classes) |
| `simulated` | the column holding the values produced by the simulation |
| `references` | the columns holding the reference values (census, survey, ...) |

An output name contains only letters, digits, `_` and `-`. Names that differ
only by case, and names reserved by Windows, are refused on every system.

### Known outputs and their files

eqasim-sa knows the twelve files that eqasim writes in its standard stages.
For these names, `file` and `separator` have defaults, so `persons: {}` is
enough:

| Outputs | Default `file` | Default `separator` | Written by the eqasim stage |
| --- | --- | --- | --- |
| `persons`, `households`, `activities`, `trips`, `vehicle_types`, `vehicles` | `{prefix}<name>.csv` | `;` | `synthesis.output` |
| `age`, `age_purpose`, `distance`, `license`, `nbr_vehicle`, `travel` | `analysis_population/{prefix}<name>.csv` | `,` | `analysis.synthesis.population` |

`{prefix}` stands for the `output_prefix` of your eqasim configuration (eqasim's
default is `ile_de_france_`); it is the only placeholder allowed in `file`.
For any other name, `file` is required and `separator` defaults to `,`. This is
how the starters keep the spatial activities file:

```yaml
outputs:
  activities_spatial:
    file: "{prefix}activities.gpkg"
```

`file` is a relative path written with `/`, without `..`, `\`, `:`, or an
absolute path. You may also change the `file` of a known output, for example
to read a file written by your own eqasim stage under a known name; the stage
check described in [What eqasim runs](#what-eqasim-runs) then does not apply
to it.

Loading also refuses, with a message naming the output:

- a `file` whose extension, once `{prefix}` is replaced, cannot name an archive
  file (letters, digits, `_` and `-` only, for example `.csv~` is refused);
- when your eqasim configuration sets `config.output_formats`, a known
  population output (`persons`, `households`, `activities`, `trips`,
  `vehicle_types`, `vehicles`) left at its default `.csv` file while
  `output_formats` lacks `csv`, or a `.gpkg` output while it lacks `gpkg`;
  eqasim would not write that file;
- an indicator (`share`, `value`, `tv_distance`) on an output whose `file` is
  not a `.csv` file: indicators read CSV tables only.

### What is archived

At the end of every successful run, each declared file is copied byte for byte
into the run archive, under the name `<output name><file extension>`: `age.csv`,
`persons.csv`, `activities_spatial.gpkg`, and so on. The run manifest records,
for each file, its original path under the eqasim output folder (for example
`analysis_population/nantes_age.csv`), its size, and its SHA-256. A declared
file that eqasim did not write makes the run fail with a message that names the
expected path and reminds you to check `run:` in your eqasim configuration.
Files that are not declared are not kept.

Only `file` and `separator` determine what is archived, so only these two keys,
with the output name, are sealed in the plan. `category`, `simulated`, and
`references` concern the analysis: you may add or correct them after the runs
and recompute the analysis without running anything again. Adding, removing, or
renaming an output, or changing its `file` or `separator`, needs a new
campaign; resuming the old one stops with a message such as
`output 'age' changed since plan: separator`.

### Columns read by indicators

`category`, `simulated`, and `references` name columns of a CSV output. They
are needed only for the outputs used by `tv_distance` or `share`;
`category` and `simulated` are distinct, non-empty strings, and `references` is
a possibly empty, duplicate-free list of non-empty strings distinct from the
two other columns. The names must reproduce the CSV header exactly, including
spaces and case.

CSV files are read as text first. If the first column has an empty header (the
row index pandas writes), it is dropped. The columns an indicator uses as
numbers (`simulated`, the reference columns, the column read by `value`) are
then converted explicitly; a cell that is not a finite number stops the
analysis with an error naming the column. Class labels stay text: a class
written `0` in the file is the string `"0"`.

To see the exact column names, open an archived file in a spreadsheet or print
its first line:

```bash
head -n 1 campaigns/mon-analyse-sa-calibration/archives/<runId>/age.csv
```

Replace `<runId>` with an actual identifier.

## `indicators`

An indicator turns the outputs of one run into a single number, which the
sensitivity analysis then studies. Three kinds are built in:

| Kind | Name of the result | What it computes |
| --- | --- | --- |
| `tv_distance` | `tv_distance(age, ref=INSEE)` | total variation distance between the simulated and a reference distribution |
| `share` | `share(age, 0-10)` | proportion of one class in a simulated distribution |
| `value` | `value(summary, mean_income, zone=44)` | one number read in one cell of a table |

### Default

Without an `indicators` key, eqasim-sa computes `tv_distance` for every output
against each of its references. Outputs declared without references are
skipped. With the outputs of the starters, this gives three indicators:
`tv_distance(age, ref=INSEE)`, `tv_distance(distance, ref=HTS)`, and
`tv_distance(nbr_vehicle, ref=HTS)`.

`indicators: []` disables indicators. Only Grid accepts no indicator; discrete
OAT, canonical Morris, Sobol, and multi-scramble Sobol require at least one.

### Choosing indicators

Each entry of the list has exactly one key, the kind of indicator:

```yaml
indicators:
  - tv_distance: {output: age, reference: INSEE}     # one output against one reference
  - tv_distance: {output: distance}                  # one output against each of its references
  - tv_distance: {outputs: [distance, nbr_vehicle]}  # several outputs, each against each of its references
  - share: {output: nbr_vehicle, category: "0"}      # proportion of one class
  - value: {output: summary, column: mean_income, where: {zone: "44"}}  # one cell
```

These lines illustrate each form. Written together they would compute
`tv_distance(distance, ref=HTS)` twice, which is refused: a real list names
each indicator once.

The block form is equivalent to the inline form:

```yaml
indicators:
  - tv_distance:
      output: age
      reference: INSEE
```

Every indicator gets a name, shown in the first table; a scientific charter's
`primary_indicator` must equal one of these names. Resolved indicator names
must be unique within the analysis.

### Total variation distance (`tv_distance`)

A `tv_distance` entry has exactly `output` and `reference`, or only `output`,
or only `outputs`. Names are non-empty strings; `outputs` is a non-empty,
duplicate-free list. Every output must be declared in `outputs` with
`category` and `simulated`, every reference must be one of that output's
references, and an output named without a reference must have at least one.

The indicator reads the `category`, `simulated`, and reference columns. Both
mass columns must be finite numbers, non-negative, and have a positive sum.
Class labels must be non-empty and unique. The two columns are normalized
separately, then:

`TV = 0.5 × Σ |p_i - q_i|`, with a result bounded in `[0, 1]`.

0 means identical distributions; 1 means distributions with no class in common.

### Share of a class (`share`)

```yaml
indicators:
  - share: {output: age, category: "0-10"}
```

A `share` entry has exactly `output` and `category`. The output must declare
`category` and `simulated`. The simulated column is normalized to sum to 1, and
the indicator is the proportion of the row whose class equals `category`. Its
name is `share(age, 0-10)`.

`category` must be a quoted YAML string, even when the class looks like a
number or a boolean: write `category: "0"` or `category: "True"`, never
`category: 0`. A class absent from the file stops the analysis with an error.

### One number from a table (`value`)

```yaml
indicators:
  - value: {output: summary, column: mean_income}
  - value: {output: summary, column: mean_income, where: {zone: "44"}}
```

A `value` entry has `output` and `column`, and optionally `where`. It reads the
cell of `column` in the single row selected by `where`. Without `where`, the
file must have exactly one row. With `where`, every listed column must equal the
given text; exactly one row must match. The cell must be a finite number. The
names are `value(summary, mean_income)` and `value(summary, mean_income,
zone=44)`.

Like `share.category`, every value in `where` must be a quoted YAML string
(`zone: "44"`, not `zone: 44`). The output needs no `category`, `simulated`, or
`references`.

### Your own calculations

When you need a quantity that the three kinds above do not compute (a mean
income, a rate per zone, a modal share), compute it inside eqasim: write an eqasim analysis stage that
saves a small CSV table in the output folder, add that stage to `run:` in your
eqasim configuration, declare the file under `outputs`, and read it with
`value`. For example, if your stage writes
`analysis_population/{prefix}summary.csv`:

```yaml
outputs:
  summary:
    file: "analysis_population/{prefix}summary.csv"
indicators:
  - value: {output: summary, column: mean_income}
```

Because the stage belongs to the eqasim code, it is part of the code
fingerprint and of the campaign identity, and its CSV file is archived with
every run.

## `execution`

```yaml
execution:
  scratch: ../scratch
  workers: 2
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

The block describes how runs are executed on this machine. It is optional for
`plan`, `analyze`, and `show-config`, but required for `run`, `check`, and
`estimate`.

| Key | Presence | Content |
| --- | --- | --- |
| `scratch` | required | temporary working directory for the runs |
| `backend` | optional, default `local` | `local` or `slurm` |
| `workers` | optional, default `1` | number of runs executed in parallel |
| `invocation` | optional, default `[uv, run, --locked, python, -m, synpp]` | command that starts synpp |
| `machine_options` | optional, default empty | eqasim environment options for this machine |
| `slurm` | optional | cluster resources, only with `backend: slurm` |

`workers` is an integer in `[1, 1024]`. `scratch` is a non-empty string without
NUL or symbolic-link traversal that must already designate a directory.
`invocation` is a non-empty list of non-empty strings without NUL.

### `machine_options`

These are eqasim options that depend on the machine, not on the study. Only
options present in the sealed model YAML are allowed: `data_path`,
`git_binary`, `java_binary`, `java_memory`, `maven_binary`, and `processes`. The
value must retain exactly the type from the model YAML and be transportable as a
synpp scalar; booleans are not. `data_path` must resolve to an existing
directory without traversing a symbolic link. For SLURM, `processes` must be a
strictly positive integer.

Machine options do not contribute to the `runId`: they describe
infrastructure, not a scientific factor. They are applied only at execution
time. A scientific parameter such as `sampling_rate` is refused here; vary it
under `parameters`.

### `slurm`

```yaml
execution:
  backend: slurm
  workers: 20
  scratch: /scratch/mon-etude
  machine_options:
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
same pattern as `campaign`. `cpus_per_task` comes from
`machine_options.processes`, or is 1.

## `calibration`

```yaml
calibration:
  meta_configs: [00-calibration.yml]
```

The mapping contains exactly `meta_configs`, a non-empty list of strings. The
paths designate regular files, are deduplicated, cannot refer to the file itself,
and cannot in turn contain a `calibration` block. Additional paths can be passed
to `check` and `estimate` through the CLI.

## `charter`

The optional charter classifies what a campaign may claim. Omitting it means a
demonstration charter. An explicit demonstration has exactly this
form, with no additional key:

```yaml
charter:
  status: demonstration
```

A demonstration campaign validates configuration, execution, storage, and
analysis plumbing. It cannot support inferential or decision claims. Existing
Grid, discrete OAT, and `sobol` are demonstration designs. Discrete OAT is the
adjacent-level demonstration and is not canonical Morris. The supported methods
are `grid`, `discrete_oat`, `canonical_morris`, `sobol`, and
`multiscramble_sobol`.

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
inventory must exactly equal the names declared under `parameters`, plus `seed`
for multi-scramble Sobol, whose seed factor comes from `experiment.seeds`.

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

It has no allocation. The seed factor, named `seed` in the charter and built
from `experiment.seeds`, remains a required varied item of the parameter
inventory, but it is a finite nuisance support: neither controllable importance
nor intrinsic simulator noise is identified.

The charter is structurally validated by `load_meta_config`. Current scientific
methods are `canonical_morris` for independent-input `screening` with
`conditional_mean`, and `multiscramble_sobol` for independent-input `fp` or `ff`
with `finite_seed_factor`. Discrete OAT and demonstration
Sobol (`sobol`) are rejected by scientific charters.

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

## Complete valid examples

The examples below are complete. Before loading them, eqasim-sa also checks the
machine: the paths must exist, `model.root` must be a directory containing the
eqasim code, `model.config` must be a regular file whose `run:` list produces
the declared known outputs, the manifest and lockfile must be regular files,
the scratch directory must exist, and factors and machine options must exist in the model YAML with
the indicated types. The referenced `00-calibration.yml` file must also exist
and must not contain a `calibration` block.

The files rendered by `init` are demonstration starters:
[calibration](../src/eqasim_sa/templates/00-calibration.yml),
[Grid](../src/eqasim_sa/templates/01-grid.yml),
[discrete OAT](../src/eqasim_sa/templates/02-discrete-oat.yml), and
[Sobol](../src/eqasim_sa/templates/03-sobol.yml). There is no generated
canonical Morris or multi-scramble Sobol starter; the
[Nantes scientific protocol](../protocols/nantes-scientific/README.md) contains
complete scientific files.

### Grid

The Grid starter is shown in [A first file](#a-first-file).

### The same Grid with every default written

This is what `show-config configs/01-grid.yml` prints for that starter. It is
also a valid meta-configuration: the two files describe the same campaign.

```yaml
campaign: mon-analyse-sa-grid
campaign_root: ../campaigns/mon-analyse-sa-grid
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
experiment:
  method: grid
  seeds:
  - 1201
  - 1202
parameters:
  sampling_rate:
    values:
    - 0.001
    - 0.002
analysis:
  completeness_threshold: 1.0
outputs:
  persons:
    file: '{prefix}persons.csv'
    separator: ;
  households:
    file: '{prefix}households.csv'
    separator: ;
  trips:
    file: '{prefix}trips.csv'
    separator: ;
  activities:
    file: '{prefix}activities.csv'
    separator: ;
  activities_spatial:
    file: '{prefix}activities.gpkg'
    separator: ','
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references:
    - INSEE
    file: analysis_population/{prefix}age.csv
    separator: ','
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references:
    - HTS
    file: analysis_population/{prefix}distance.csv
    separator: ','
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references:
    - HTS
    file: analysis_population/{prefix}nbr_vehicle.csv
    separator: ','
indicators:
- tv_distance:
    outputs:
    - age
    - distance
    - nbr_vehicle
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
  backend: local
  invocation:
  - uv
  - run
  - --locked
  - python
  - -m
  - synpp
calibration:
  meta_configs:
  - 00-calibration.yml
```

In this printed form, the known outputs show their default `file` and
`separator`.

### Discrete OAT

```yaml
# Discrete adjacent-level OAT demonstration: eight positions and six unique runs, not Morris inference.
campaign: mon-analyse-sa-discrete_oat
experiment:
  method: discrete_oat
  seeds: [1301, 1302]
  trajectories: 2
  design_seed: 20260817
parameters:
  sampling_rate: [0.001, 0.002, 0.003]
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
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
calibration:
  meta_configs: [00-calibration.yml]
# --- Rempli par eqasim-sa init : à modifier seulement si la machine change ---
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

### Demonstration Sobol

```yaml
# Single-scramble Sobol demonstration: 48 positions and 4 unique runs, not inferential evidence.
campaign: mon-analyse-sa-sobol
experiment:
  method: sobol
  seeds: [1401, 1402]
  base_sample_size: 8
parameters:
  sampling_rate: [0.001, 0.002]
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
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
analysis:
  bootstrap_resamples: 99
calibration:
  meta_configs: [00-calibration.yml]
# --- Rempli par eqasim-sa init : à modifier seulement si la machine change ---
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

### Grid with chosen indicators

This variant uses a copy of the Nantes eqasim configuration named
`config_nantes_sa.yml` (made with `cp config_nantes.yml config_nantes_sa.yml`)
next to `config_nantes.yml`, in which `matching_minimum_age` is written under
`config:` and `analysis.synthesis.population` is in `run:`. It keeps only two
distributions, chooses its indicators, writes its own campaign folder, and uses
the long form for one parameter:

```yaml
campaign: mon-analyse-sa-grid-age
campaign_root: ../campaigns/grid-age
experiment:
  method: grid
  seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
  matching_minimum_age: [0, 5, 12]
outputs:
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
indicators:
  - tv_distance: {output: age, reference: INSEE}
  - tv_distance: {output: nbr_vehicle}
  - share: {output: nbr_vehicle, category: "0"}
model:
  root: ../../eqasim-france
  config: config_nantes_sa.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
```

## Invalid examples

Each fragment below makes an otherwise valid file fail, with the message shown.

A seed declared as a parameter:

```yaml
parameters:
  seed: [1, 2]
```

> declare simulation seeds in experiment.seeds, not parameters.seed

A misspelled experiment key (`seed` instead of `seeds`):

```yaml
experiment:
  method: grid
  seed: [1201, 1202]
```

> experiment (grid) keys must be among method, seeds; unknown: seed

An analysis key that belongs to another method:

```yaml
analysis:
  bootstrap_resamples: 999
```

> analysis keys must be among completeness_threshold; unknown: bootstrap_resamples

An indicator on an undeclared output:

```yaml
indicators:
  - tv_distance: {output: income}
```

> invalid indicator combinations: tv_distance(income)

An output that eqasim-sa does not know, declared without its file:

```yaml
outputs:
  summary: {}
```

> output 'summary' is not a known eqasim output; declare its file (relative to
> eqasim's output_path, e.g. analysis_population/{prefix}summary.csv)

A `share` class written as a YAML number instead of a quoted string:

```yaml
indicators:
  - share: {output: nbr_vehicle, category: 0}
```

> share category must be a quoted string (e.g. "0", "True")

A `where` value written as a YAML number:

```yaml
indicators:
  - value: {output: summary, column: mean_income, where: {zone: 44}}
```

> where values must be quoted strings

A scientific parameter given as a machine option:

```yaml
execution:
  machine_options:
    sampling_rate: 0.002
```

> machine_options 'sampling_rate' is not an allowed environment option

A short-form parameter under a scientific charter (the charter itself is
omitted here):

```yaml
parameters:
  sampling_rate: [0.001, 0.002]
```

> factor 'sampling_rate' requires a distribution under a scientific charter (use
> the long form with values and distribution)

An unordered domain in the discrete OAT file, because discrete OAT moves
between adjacent increasing levels:

```text
parameters:
  sampling_rate: [0.001, 0.003, 0.002]
```

> discrete OAT factor 'sampling_rate' levels must be distinct and strictly
> increasing

A duplicate key (here also using a YAML alias, which is refused too) is
detected before any other check:

```text
parameters: &params {}
parameters: *params
```

> duplicate YAML key: 'parameters'
