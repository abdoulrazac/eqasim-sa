# Getting started

**English** | [Français](../docs-fr/getting-started.md)

## Prerequisites

- Linux/POSIX: the code uses `fcntl`, `flock`, `os.uname`, and process groups, among other features.
- Exactly Python 3.13 within the declared range `>=3.13,<3.14`.
- `uv` and Git available.
- A clean eqasim repository with a `HEAD` commit, a YAML file tracked by Git, and a `uv.lock`.
- An existing data directory without symbolic links for `init`.
- The synpp environment required by eqasim. `synpp` is not a Python dependency of `eqasim-sa`.

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
| Grid | `plan`: `created`, 8 positions, 8 unique runs; `run`: 8 completed; `analyze`: `completed` | `indicators.parquet`, `replication-dispersion.parquet`, `output-summary.parquet`, `governed-report.json`, `analysis-generation.json` |
| Discrete OAT | `plan`: `created`, 8 positions, 6 unique runs; `run`: 6 completed; `analyze`: sensitivity `discrete_oat` | `indicators.parquet`, `replication-dispersion.parquet`, `discrete-oat-effects.parquet`, `output-summary.parquet`, `governed-report.json`, `analysis-generation.json` |
| Sobol | `plan`: `created`, 32 positions, 8 unique runs; `run`: 8 completed; `analyze`: sensitivity `sobol` | `indicators.parquet`, `sobol-indices.parquet`, `output-summary.parquet`, `governed-report.json`, `analysis-generation.json` |

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
