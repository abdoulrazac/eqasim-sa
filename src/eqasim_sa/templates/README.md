# @PROJECT_NAME@

`eqasim-sa init` generated this project template and `manifest.json`. It did not
plan a campaign, run synpp, execute the pipeline, estimate resources, or analyze
results. Every command below is an explicit manual step.

## Generated layout

- `manifest.json`: hash manifest of the selected input data tree.
- `configs/00-calibration.yml`: three resource-calibration positions.
- `configs/01-grid.yml`: Grid target, 8 positions and 8 unique runs.
- `configs/02-morris.yml`: Morris target configuration.
- `configs/03-sobol.yml`: Sobol target, 32 positions and 8 unique runs.
- `campaigns/`: initially empty; manual `plan` commands create campaign state.
- `scratch/`: initially empty and disposable unless `--scratch` selected another path.

Run all commands from this directory. This generated array uses the source
checkout runner when available and the installed command otherwise.

```bash
SA=(@RUNNER@)
```

## 1. Calibrate resource estimates

Resource calibration measures duration, durable storage and worker-cache size.
It is not scientific calibration of eqasim parameters. Seal and run it first so
target checks and estimates have observations. A static calibration check is
optional afterward.

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml
# Optional after the first calibration run:
"${SA[@]}" check --meta-config configs/00-calibration.yml
```

## 2. Seal target plans

```bash
"${SA[@]}" plan --meta-config configs/01-grid.yml
"${SA[@]}" plan --meta-config configs/02-morris.yml
"${SA[@]}" plan --meta-config configs/03-sobol.yml
```

## 3. Execute and analyze each target

```bash
"${SA[@]}" check --meta-config configs/01-grid.yml
"${SA[@]}" estimate --meta-config configs/01-grid.yml
"${SA[@]}" run --meta-config configs/01-grid.yml
"${SA[@]}" analyze --meta-config configs/01-grid.yml

"${SA[@]}" check --meta-config configs/02-morris.yml
"${SA[@]}" estimate --meta-config configs/02-morris.yml
"${SA[@]}" run --meta-config configs/02-morris.yml
"${SA[@]}" analyze --meta-config configs/02-morris.yml

"${SA[@]}" check --meta-config configs/03-sobol.yml
"${SA[@]}" estimate --meta-config configs/03-sobol.yml
"${SA[@]}" run --meta-config configs/03-sobol.yml
"${SA[@]}" analyze --meta-config configs/03-sobol.yml
```

Never edit a `plan.parquet` or completion marker. Rerunning the same campaign
command executes only missing work. Use a new `campaign` and `campaign_root` for
a changed design, and a new project when the sealed model or data context changes.

Grid and Sobol vary the scalar `matching_age_scheme` between `matching_6` and
`analysis_7`. Morris does not because it accepts only numeric factors. The model
configuration keeps `analysis_age_scheme: analysis_7` fixed so output classes
remain comparable; changing an output scheme is not a matching effect.

The Nantes model also exposes optional scalar factors:

| Factor | Default | Example domain | Compatible methods |
| --- | ---: | --- | --- |
| `matching_minimum_observations` | `20` | `[5, 20, 50]` | Grid, Morris, Sobol |
| `matching_minimum_age` | `5` | `[0, 5, 12]` | Grid, Morris, Sobol |
| `income_assignation_method` | `uniform` | `[uniform, bhepop2]` | Grid, Sobol |

Adding `matching_minimum_observations` to the generated Grid gives 24 positions:
two rates by two matching schemes by three thresholds by two replication seeds.
Use a new `campaign` and `campaign_root` for that modified design. The starter
indicators do not measure income directly. The `households` artifact already
contains household income; register a custom scalar indicator on it before
interpreting `income_assignation_method`. Note that `bhepop2` can fall back to
`uniform` for municipalities with invalid Filosofi distributions, and the
current output does not retain the applied method.

Before generating this project, `init` verified that the model configuration
explicitly contains `matching_age_scheme: matching_6` and
`analysis_age_scheme: analysis_7`.
