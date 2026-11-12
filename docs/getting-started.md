# Getting started

**English** | [Français](../docs-fr/getting-started.md)

## Prerequisites

- Windows 10 or later, macOS 13 or later, or Linux (supported; verification on Windows and macOS is pending, see [Platform verification](platform-verification.md)). The `local` backend behaves the same on all three; the `slurm` backend requires Linux (see [Troubleshooting](troubleshooting.md#slurm-backend-on-windows-or-macos)). No administrator rights are needed, except on Windows without long paths, where a real eqasim run needs an administrator to enable them once (see [Short scratch on Windows](#short-scratch-on-windows)).
- Campaigns, scratch, and data on a local NTFS, APFS, ext4, or xfs disk, or on the cluster's shared filesystem. FAT/exFAT volumes and SMB shares are not supported for campaigns (not detected: eqasim-sa does not refuse them, so check where your paths live).
- Exactly Python 3.13 within the declared range `>=3.13,<3.14`.
- `uv` and Git available.
- A clean eqasim repository with a `HEAD` commit, a YAML file tracked by Git, and a `uv.lock`.
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

The `eqasim-sa` lockfile pins the runner environment. The lockfile referenced
in a meta-configuration, however, is part of the sealed scientific context.

## Initialize a project

```bash
"${SA[@]}" init \
  --project-name mon-analyse-sa \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
```

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
not modify the YAML files, plans, or results. For an existing project, `init`
first checks that the static files still exactly match the expected templates
and that the durable state has a recognized structure. Regeneration returns
`status: initialized`. If a plan already exists, the manifest's new SHA-256
changes the expected context: retain the old state and use new campaign roots
or a new project.

## Generated project runner

The generated README defines a Bash array:

```bash
SA=(...)
```

From a source checkout, the rendered command uses
`uv run --project /chemin/vers/eqasim-sa --locked eqasim-sa`. In an installed
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

`check` without `--smoke` validates the meta-configuration, plan, environment,
and estimated space. `check --smoke` adds two actual temporary runs and should
therefore be reserved for explicit pipeline validation. It does not apply
`execution.overrides`: it starts from the sealed model YAML, forces
`sampling_rate: 0.001` and the first two seeds. In particular, its `data_path`
is the model's value, not `execution.overrides.data_path`.

The provided templates are small demonstration campaigns. Their sizes are not a
scientific recommendation, and `init` deliberately generates no canonical
Morris starter. Consult [Parameters and methods](parameters-and-methods.md)
before expanding domains or authoring a study-specific scientific campaign.
Use the short guides for [Grid](what-when-how-grid.md),
[historical Morris](what-when-how-morris.md), [discrete OAT](what-when-how-discrete_oat.md),
[canonical Morris](what-when-how-canonical_morris.md), [historical Sobol](what-when-how-sobol.md),
and [multi-scramble Sobol](what-when-how-multiscramble_sobol.md) to choose the
correct method and claim boundary.

## First structured target

To compare named ordered `matching_attributes` lists, first commit an active
list-valued key in the clean model repository, then derive a Grid configuration
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
| Calibration | `plan`: `created`, 3 positions, 3 unique runs; `run`: `completed`, 3 completed | `campaigns/calibration/plan.parquet`, 3 markers under `runs/`, 3 archives |
| Grid | `plan`: `created`, 8 positions, 8 unique runs; `run`: 8 completed; `analyze`: `completed` | `analysis-generation.json`; selected generation: `indicators.parquet`, `replication-dispersion.parquet`, `output-summary.parquet`, `governed-report.json` |
| Discrete OAT | `plan`: `created`, 8 positions, 6 unique runs; `run`: 6 completed; `analyze`: sensitivity `discrete_oat` | `analysis-generation.json`; selected generation: `indicators.parquet`, `replication-dispersion.parquet`, `discrete-oat-effects.parquet`, `output-summary.parquet`, `governed-report.json` |
| Sobol | `plan`: `created`, 32 positions, 8 unique runs; `run`: 8 completed; `analyze`: sensitivity `sobol` | `analysis-generation.json`; selected generation: `indicators.parquet`, `sobol-indices.parquet`, `output-summary.parquet`, `governed-report.json` |

An identical rerun of `plan` returns `unchanged`. For an already complete
campaign, `run` returns `status: completed` and `total: 0`.

## Completion checklist

- The four plans match the expected cardinalities or your documented design.
- Every expected `runId` has a valid marker and a readable archive.
- `analyze` reports `status: completed` and the expected completion rate.
- The schemas and columns of the artifact Parquet files match the YAML declarations.
- The Grid, discrete OAT, and Sobol demonstration outputs have been inspected, not merely created.
- The generated Sobol campaign is historical and noninferential. A scientific
  multi-scramble Sobol campaign must be authored separately; there is no starter.
- The model, data, lockfile, and `eqasim-sa` version are archived or traceable.

See also the [glossary](glossary.md).
