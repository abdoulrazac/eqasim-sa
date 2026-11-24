# Getting started

**English** | [Français](../docs-fr/getting-started.md)

## Prerequisites

- Windows 10 or later, macOS 13 or later, or Linux (supported; the suite is tested on Linux, Python 3.13: 1558 passed, 4 skipped; the GitHub Actions CI workflow `.github/workflows/tests.yml` runs it on Ubuntu, macOS, and Windows, and no Windows or macOS result is claimed until CI has run). The `local` backend behaves the same on all three; the `slurm` backend requires Linux (see [Troubleshooting](troubleshooting.md#slurm-backend-on-windows-or-macos)). No administrator rights are needed, except on Windows without long paths, where a real eqasim run needs an administrator to enable them once (see [Short scratch on Windows](#short-scratch-on-windows)).
- Campaigns, scratch, and data on a local NTFS, APFS, ext4, or xfs disk, or on the cluster's shared filesystem. FAT/exFAT volumes and SMB shares are not supported for campaigns (not detected: eqasim-sa does not refuse them, so check where your paths live).
- Exactly Python 3.13 within the declared range `>=3.13,<3.14`.
- `uv` available. Git is not required.
- An eqasim folder (a clone, a copy, or an unpacked archive) containing its configuration file (by default `config.yml`) and a `uv.lock`.
- An existing data directory without symbolic links for `init`.
- The synpp environment required by eqasim. `synpp` is not a Python dependency of `eqasim-sa`.

## Install uv without administrator rights

Linux and macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv` installs into your user profile and downloads Python 3.13 itself when
needed. The command examples in this guide use Bash syntax. On Windows, run them
from Git Bash, or in PowerShell write the full `uv run --project <path>
--locked eqasim-sa` command wherever `"${SA[@]}"` appears.

## Install with uv

At the root of the `openmole-eqasim` repository:

```bash
ROOT="$PWD"
uv sync --project "$ROOT/eqasim-sa" --extra test --locked
uv sync --project "$ROOT/eqasim-france" --locked
SA=(uv run --project "$ROOT/eqasim-sa" --locked eqasim-sa)
```

To check the installation, `"${SA[@]}" --version` prints the installed version,
for example `eqasim-sa 0.4.1`.

Without a source checkout, install a release published on GitHub as a uv tool:

```bash
uv tool install git+https://github.com/abdoulrazac/eqasim-sa@v0.4.1
SA=(eqasim-sa)
```

This pins eqasim-sa's direct dependencies; their own dependencies take the
newest compatible versions. To install exactly the versions tested for the
release, add the `constraints.txt` file attached to it:

```bash
uv tool install --constraints https://github.com/abdoulrazac/eqasim-sa/releases/download/v0.4.1/constraints.txt \
  git+https://github.com/abdoulrazac/eqasim-sa@v0.4.1
```

The `eqasim-sa` lockfile pins the runner environment. The lockfile referenced
in a meta-configuration, however, is part of the sealed scientific context.

The folders `eqasim-sa/`, `eqasim-france/`, and `eqasim-data/` side by side
under one root are only an example. Each can be anywhere, as long as they are
not nested in one another (`init` refuses a project inside the eqasim folder or
the reverse, a scratch inside the eqasim folder, and a project inside its
scratch): give absolute paths
to `--project`, `--model-root`, `--data-path`, `--directory`, and `--scratch`.
`init` writes the paths of the starters relative to their `configs/` folder
(absolute on Windows when the folder is on another drive); if you later move
the project without those folders, edit the paths under the "Machine-specific
values" comment of each starter.

## Initialize a project

```bash
"${SA[@]}" init \
  --project-name mon-analyse-sa \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
```

`--model-config` names the eqasim configuration of your study, relative to
`--model-root` or absolute; it defaults to `config.yml`, the official
Île-de-France file. Work on a copy made in the eqasim folder, so that the
official files stay untouched: `cp config.yml my_config.yml` in general, or,
for Nantes, `cp config_nantes.yml config_nantes_sa.yml` (eqasim ships
`config_nantes.yml` itself; do not overwrite it). Then add
`--model-config my_config.yml` (or `--model-config config_nantes_sa.yml`); the
starters contain `config: my_config.yml` (or `config: config_nantes_sa.yml`).

`init` is strictly an atomic template generator. It renders four YAML files, a
README, and a `manifest.json` containing the size and SHA-256 of every data
file. It does not start any subprocess, check the synpp environment, plan
anything, or run any pipeline stage.

```text
mon-analyse-sa/
├── .gitattributes
├── README.md
├── manifest.json
├── configs/
│   ├── 00-calibration.yml
│   ├── 01-grid.yml
│   ├── 02-discrete-oat.yml
│   └── 03-sobol.yml
├── campaigns/
└── scratch/
```

`init` also writes a `.gitattributes` that forces LF line endings for `*.yml`,
`*.yaml`, `*.json`, and `*.lock`, so that a Windows checkout with CRLF conversion
does not change sealed inputs. It never overwrites an existing `.gitattributes`;
a missing one is recreated and reported under `restored`.

An identical rerun returns `status: unchanged` without rehashing the data.
`--regenerate-data-manifest` atomically replaces the local manifest but does
not modify the YAML files, plans, or results; it returns `status: initialized`.
Both work after you have planned, run, and analyzed the starters: before
writing anything, `init` accepts the generated files, the starters' campaign
folders (`campaigns/mon-analyse-sa-calibration`, `-grid`, `-discrete_oat`,
`-sobol`) with what eqasim-sa wrote in them (including `figures/`), and
`configs/analyses/` for your analysis scripts. An edited starter (for example
with `analyses` added) or README is kept as it is and listed in `kept`; a
missing one is recreated and listed in `restored`. Only a change of a value
that `init` fills in (`model.root`, `model.config`, `model.data_manifest`,
`execution.scratch`, `execution.invocation`,
`execution.machine_options.data_path`, `campaign`, `campaign_root`) is refused,
naming it: `init inputs changed: model.root in configs/01-grid.yml`. These
values are compared as written (after defaults), so an absolute `model.root`
pointing to the same folder counts as a change. Another
file in `configs/` or a campaign folder of your own is refused too. The
[CLI reference](cli-reference.md#rerunning-init-on-an-existing-project) gives
the exact rules. You never need to rerun `init` to continue working.

After a change of the data tree, refresh the manifest with
`--regenerate-data-manifest`: an identical rerun does not rehash the data, so
only this option detects a data change. If the data changed, a campaign planned before
the regeneration no longer
matches its plan: `run` refuses to resume it with `data manifest changed since
plan (dataManifestSha256)`. Keep the old state and use new campaign roots or a
new project.

## Generated project runner

The generated README defines a Bash array:

```bash
SA=(...)
```

From a source checkout, the rendered command uses
`uv run --project /path/to/eqasim-sa --locked eqasim-sa`. In an installed
package where no source `pyproject.toml` can be detected, it uses `eqasim-sa`.
Always invoke the array with `"${SA[@]}"` to preserve the arguments.

## Short scratch on Windows

Windows limits paths to 260 characters unless long paths are enabled. eqasim's
own cache already creates relative paths of up to about 231 characters, so on
Windows without long paths no `execution.scratch` fits a real eqasim run: an
administrator must enable Windows long paths (`LongPathsEnabled`) once, outside
eqasim-sa, until eqasim creates shorter paths. Use a short `execution.scratch`
such as `C:\esa` and run `check` before `run`; it reports this case before any
long run. See [Troubleshooting](troubleshooting.md#path_length-check-fails-on-windows).

## Read a generated configuration

Each file in `configs/` is short: the campaign name, the method and its seeds,
the parameter values to try, the model outputs to compare with reference data,
and, under a comment line, the paths that `init` filled in for this machine.
Everything else takes a default value. The
[YAML reference](yaml-reference.md#a-first-file) reads the Grid starter line by
line. To see the complete configuration that eqasim-sa will use, with every
default written out:

```bash
"${SA[@]}" show-config configs/01-grid.yml
```

`show-config` prints YAML, not JSON. If the file is invalid, it prints the error
on standard error and exits with code 1.

## Prepare your eqasim configuration

The starters work with the official, unmodified eqasim. Before the first
`plan`, check two things in the eqasim configuration named by `model.config`:

- Its `run:` list must contain `analysis.synthesis.population`. The starters
  compare the `age`, `distance` and `nbr_vehicle` distributions with census and
  survey data, and these files are written by that eqasim stage. The official
  `config.yml` lists only `synthesis.output`; add the second stage:

  ```yaml
  run:
    - synthesis.output
    - analysis.synthesis.population
  ```

  eqasim-sa runs `run:` exactly as written. If the stage is missing, loading
  stops with `output 'age' is written by eqasim stage
  'analysis.synthesis.population', which is not in run: of ...`.
- Every parameter you vary must be written in its `config:` section. The
  starters vary only `sampling_rate`, which the official configuration
  contains. To vary another one, for example `matching_minimum_age`, write it in
  `config:` first.

Changing this file after a plan has been sealed changes the campaign identity:
make these edits before planning, or use a new campaign afterwards.

## First resource calibration

From `mon-analyse-sa/`, seal and then run the calibration:

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml
```

The first `run` provides the observations required by `check` and `estimate`:
runtime, durable bytes, and maximum cache size. A static check can then be run,
but it is not a prerequisite for bootstrapping:

```bash
"${SA[@]}" check --meta-config configs/00-calibration.yml
```

## First target

Once the calibration observations are available, follow this order:

```bash
TARGET=configs/01-grid.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

Optionally, once `analyze` has published, draw the figures:

```bash
"${SA[@]}" plot --meta-config "$TARGET"
```

`plot` writes the PNG figures valid for the method, plus an index `figures.json`, to `figures/<generationId>/`; it does not touch `analysis/`. See [Figures](workflow-and-results.md#figures).

`check` without `--smoke` validates the meta-configuration, plan, environment,
and estimated space. `check --smoke` adds two actual temporary runs and should
therefore be reserved for explicit pipeline validation. It starts from the
configuration of the first run of the plan, with `execution.machine_options`
applied as for `run` (so `execution.machine_options.data_path` is used), then
forces `sampling_rate: 0.001` and the first two distinct seeds.

The provided templates are small demonstration campaigns. Their sizes are not a
scientific recommendation, and `init` deliberately generates no canonical
Morris starter. Consult [Parameters and methods](parameters-and-methods.md)
before expanding domains or authoring a study-specific scientific campaign.
Use the short guides for [Grid](what-when-how-grid.md),
[discrete OAT](what-when-how-discrete_oat.md),
[canonical Morris](what-when-how-canonical_morris.md), [demonstration Sobol](what-when-how-sobol.md),
and [multi-scramble Sobol](what-when-how-multiscramble_sobol.md) to choose the
correct method and claim boundary.

## First structured target

To compare named ordered `matching_attributes` lists, first write an active
list-valued key in your eqasim configuration, then derive a Grid configuration
under a new campaign/root. The
[structured-configurations tutorial](structured-configurations-tutorial.md)
covers the complete six-run workflow, plan metadata, analysis inspection, and
scientific-method boundaries. `init` does not modify the model YAML or generate
this target automatically.

## Minimum checkpoints for generated templates

The `plan` commands return a JSON object with `status`, `designPositions`, and
`uniqueRuns`. With the unmodified templates generated by `init`, verify:

| Stage | Minimum JSON on first execution | Expected files after `run`/`analyze` |
| --- | --- | --- |
| Calibration | `plan`: `created`, 3 positions, 3 unique runs; `run`: `completed`, 3 completed | `campaigns/mon-analyse-sa-calibration/plan.parquet`, 3 markers under `runs/`, 3 archives with eight files each (`persons.csv`, ..., `activities_spatial.gpkg`, `age.csv`, `distance.csv`, `nbr_vehicle.csv`) |
| Grid | `plan`: `created`, 4 positions, 4 unique runs; `run`: 4 completed; `analyze`: `completed` | `analysis-generation.json`; selected generation: `indicators.parquet`, `replication-dispersion.parquet`, `output-summary.parquet`, `governed-report.json` |
| Discrete OAT | `plan`: `created`, 8 positions, 6 unique runs; `run`: 6 completed; `analyze`: sensitivity `discrete_oat` | `analysis-generation.json`; selected generation: `indicators.parquet`, `replication-dispersion.parquet`, `discrete-oat-effects.parquet`, `output-summary.parquet`, `governed-report.json` |
| Sobol | `plan`: `created`, 48 positions, 4 unique runs; `run`: 4 completed; `analyze`: sensitivity `sobol` | `analysis-generation.json`; selected generation: `indicators.parquet`, `sobol-indices.parquet`, `output-summary.parquet`, `governed-report.json` |

An identical rerun of `plan` returns `unchanged`. For an already complete
campaign, `run` returns `status: completed` and `total: 0`.

## Completion checklist

- The four plans match the expected cardinalities or your documented design.
- Every expected `runId` has a valid marker and a readable archive.
- `analyze` reports `status: completed` and the expected completion rate.
- The column names of the archived CSV files match the `category`, `simulated`, and `references` declarations under `outputs`.
- The Grid, discrete OAT, and Sobol demonstration outputs have been inspected, not merely created.
- The generated Sobol campaign is a noninferential demonstration. A scientific
  multi-scramble Sobol campaign must be authored separately; there is no starter.
- The model, data, lockfile, and `eqasim-sa` version are archived or traceable.

See also the [glossary](glossary.md).
