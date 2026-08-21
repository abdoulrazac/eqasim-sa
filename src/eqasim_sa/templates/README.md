# @PROJECT_NAME@

`eqasim-sa init` generated this project template and `manifest.json`. It did not
plan a campaign, run synpp, execute the pipeline, estimate resources, or analyze
results. Every command below is an explicit manual step.

## Generated layout

- `manifest.json`: hash manifest of the selected input data tree.
- `configs/00-calibration.yml`: three resource-calibration positions.
- `configs/01-grid.yml`: Grid target, 8 positions and 8 unique runs.
- `configs/02-discrete-oat.yml`: adjacent-level discrete OAT target configuration.
- `configs/03-sobol.yml`: Sobol target, 32 positions and 8 unique runs.
- `campaigns/`: initially empty; manual `plan` commands create campaign state.
- `scratch/`: initially empty and disposable unless `--scratch` selected another path.

All four starters are demonstration campaigns. They validate configuration,
execution, and storage plumbing, but cannot support inferential or decision
claims. Discrete OAT is explicitly a demonstration design. The Sobol starter is
a legacy inferential plan; neither is scientific evidence.

A separately authored scientific campaign must include an exact `stochastic`
charter: `conditional_mean` with pilot/production replication allocation for
canonical Morris, or non-actionable `finite_seed_factor` for multi-scramble
Sobol. A canonical pilot is immutable and may only recommend a new production
campaign/root; never convert one of these generated demonstrations in place.

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
"${SA[@]}" plan --meta-config configs/02-discrete-oat.yml
"${SA[@]}" plan --meta-config configs/03-sobol.yml
```

## 3. Execute and analyze each target

```bash
"${SA[@]}" check --meta-config configs/01-grid.yml
"${SA[@]}" estimate --meta-config configs/01-grid.yml
"${SA[@]}" run --meta-config configs/01-grid.yml
"${SA[@]}" analyze --meta-config configs/01-grid.yml

"${SA[@]}" check --meta-config configs/02-discrete-oat.yml
"${SA[@]}" estimate --meta-config configs/02-discrete-oat.yml
"${SA[@]}" run --meta-config configs/02-discrete-oat.yml
"${SA[@]}" analyze --meta-config configs/02-discrete-oat.yml

"${SA[@]}" check --meta-config configs/03-sobol.yml
"${SA[@]}" estimate --meta-config configs/03-sobol.yml
"${SA[@]}" run --meta-config configs/03-sobol.yml
"${SA[@]}" analyze --meta-config configs/03-sobol.yml
```

Never edit a `plan.parquet` or completion marker. Rerunning the same campaign
command executes only missing work. Use a new `campaign` and `campaign_root` for
a changed design, and a new project when the sealed model or data context changes.

Grid and Sobol vary the scalar `matching_age_scheme` between `matching_6` and
`analysis_7`. Discrete OAT does not because it accepts only numeric factors. The model
configuration keeps `analysis_age_scheme: analysis_7` fixed so output classes
remain comparable; changing an output scheme is not a matching effect.

### Named matching configurations

Grid can also compare named ordered lists. The tracked model YAML must already
contain the active `config.matching_attributes` key as a list of strings; `init`
does not add or modify it. Commit that YAML and verify that the complete model
repository is clean before initializing or planning. Copy `configs/01-grid.yml`, give the copy a new
`campaign` and empty `campaign_root`, and replace its `parameters` block, for
example:

```yaml
campaign: @PROJECT_NAME@-grid-structured
campaign_root: ../campaigns/grid-structured
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
```

Keep the copied Grid's two replication seeds, indicators, execution, and
calibration blocks. This produces 6 positions and 6 expected unique runs. The
labels are readable aliases; execution and `runId` use the resolved ordered
lists. All Morris forms reject structured factors. This remains a demonstration,
not a variance-share analysis.

The complete walkthrough is `docs/structured-configurations-tutorial.md` in the
eqasim-sa source repository. It includes model preparation, commands, plan
inspection, interpretation, scientific caveats, and safe recovery.

Every generated configuration starts with exactly three total-variation
signals: age against INSEE, distance against HTS, and number of vehicles against
HTS. eqasim-france still archives `HTS adjust with INSEE population weights`
for compatibility, but that column is a global rescaling of HTS and therefore
has the same normalized TV value; it is intentionally not declared here.

The Nantes model also exposes optional scalar factors:

| Factor | Default | Example domain | Compatible methods |
| --- | ---: | --- | --- |
| `matching_minimum_observations` | `20` | `[5, 20, 50]` | Grid, discrete OAT, Sobol |
| `matching_minimum_age` | `5` | `[0, 5, 12]` | Grid, discrete OAT, Sobol |
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
