# @PROJECT_NAME@

`eqasim-sa init` generated this project template and `manifest.json`. It did not
plan a campaign, run synpp, execute the pipeline, estimate resources, or analyze
results. Every command below is an explicit manual step.

## Generated layout

- `manifest.json`: hash manifest of the selected input data tree.
- `configs/00-calibration.yml`: three resource-calibration positions.
- `configs/01-grid.yml`: Grid target configuration.
- `configs/02-morris.yml`: Morris target configuration.
- `configs/03-sobol.yml`: Sobol target configuration.
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
command executes only missing work. Use a new project for a changed context or
design.
