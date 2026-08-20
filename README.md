# eqasim-sa

**English** | [Français](README-fr.md)

`eqasim-sa` plans, runs, and analyzes sensitivity analysis campaigns for a
synpp-driven eqasim pipeline. Its current vocabulary is Grid, discrete OAT,
canonical Morris, and Sobol. It provides deterministic plans, per-run resumption,
indicators computed from Parquet artifacts, and local and SLURM backends.

All generated starters, including calibration, Grid, discrete OAT, and Sobol,
are demonstrations: they
validate configuration, execution, storage, and analysis plumbing but cannot
support inferential or decision claims. No canonical Morris starter is generated;
its scientific claims, uncertainty model, estimand, budget, convergence criteria,
and robustness checks must be study-specific. Historical `method: morris` remains
readable only as legacy demonstration provenance.

The generated Grid and Sobol examples vary the scalar eqasim-france
`matching_age_scheme`; discrete OAT leaves this categorical factor fixed, and
`analysis_age_scheme` remains fixed so output distributions stay comparable.
Discrete OAT uses adjacent numeric levels and local normalized effects; it is not
canonical Morris. Canonical Morris is supported only for scientific screening,
not factor prioritization or factor fixing.

The package targets exactly Python 3.13 (`>=3.13,<3.14`). From this directory:

```bash
uv sync --extra test --locked
uv run --locked eqasim-sa --help
```

## Initialize a study

From the root containing `eqasim-sa/`, `eqasim-france/`, and `eqasim-data/`:

```bash
ROOT="$(pwd -P)"
SA=(uv run --project "$ROOT/eqasim-sa" --locked eqasim-sa)

"${SA[@]}" init \
  --project-name mon-etude \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
cd "$ROOT/mon-etude"
```

`init` generates only a template project and `manifest.json`, which hashes the
data tree. It does not create a plan and does not run synpp, a campaign, or an
analysis.

## Standard workflow

```bash
# First produce the resource calibration observations.
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

# Then prepare, check, estimate, run, and analyze a target.
"${SA[@]}" plan --meta-config configs/01-grid.yml
"${SA[@]}" check --meta-config configs/01-grid.yml
"${SA[@]}" estimate --meta-config configs/01-grid.yml
"${SA[@]}" run --meta-config configs/01-grid.yml
"${SA[@]}" analyze --meta-config configs/01-grid.yml
```

Calibration covers runtime, durable storage, and worker cache. It does not
calibrate eqasim's scientific parameters.

## Documentation

- [Documentation index](docs/index.md)
- [Getting started](docs/getting-started.md)
- [CLI reference](docs/cli-reference.md)
- [YAML reference](docs/yaml-reference.md)
- [Parameters and methods](docs/parameters-and-methods.md)
- [Workflow and results](docs/workflow-and-results.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Glossary](docs/glossary.md)
- [Development guide](docs/development-guide.md)
