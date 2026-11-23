# @PROJECT_NAME@

`eqasim-sa init` generated this project template and `manifest.json`. It did not
plan a campaign, run synpp, execute the pipeline, estimate resources, or analyze
results. Every command below is an explicit manual step.

## Generated layout

- `manifest.json`: hash manifest of the selected input data tree.
- `configs/00-calibration.yml`: three resource-calibration positions.
- `configs/01-grid.yml`: Grid target, 4 positions and 4 unique runs.
- `configs/02-discrete-oat.yml`: adjacent-level discrete OAT target, 8 positions and 6 unique runs.
- `configs/03-sobol.yml`: Sobol target, 48 positions and 4 unique runs.
- `campaigns/`: initially empty; manual `plan` commands create campaign state.
- `scratch/`: initially empty and disposable unless `--scratch` selected another path.

All four starters are demonstration campaigns. They validate configuration,
execution, and storage plumbing, but cannot support inferential or decision
claims. Discrete OAT and the single-scramble Sobol starter are explicitly
demonstration designs; neither is scientific evidence.

A separately authored scientific campaign must include an exact `stochastic`
charter: `conditional_mean` with pilot/production replication allocation for
canonical Morris, or non-actionable `finite_seed_factor` for multi-scramble
Sobol. A canonical pilot is immutable and may only recommend a new production
campaign/root; never convert one of these generated demonstrations in place.

Each starter is short on purpose. `outputs` names the model outputs and their
references; `seeds` lists the replication seeds; `parameters` lists the values
tried per parameter (`name: [a, b]`). Omitted keys take defaults: the campaign
folder is `../campaigns/<campaign>`, the model lockfile is `<model root>/uv.lock`,
the Sobol design seed is derived from the campaign name (the discrete OAT
starter writes its own), the analysis uses
`completeness_threshold: 1.0` (and 999 bootstrap resamples at confidence 0.95 for
Sobol and Morris), execution is local with one worker when `workers` is absent
(the starters write `workers: 2`, two runs at a time), and when `indicators` is
absent every output with references gets a total-variation indicator. The block
under the "Rempli par eqasim-sa init" comment holds machine-specific paths; edit
it only when the machine changes.

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
command executes only missing work. Use a new `campaign` (and so a new campaign folder) for
a changed design, and a new project when the sealed model or data context changes.

## Prepare your eqasim configuration

The starters work with the official, unmodified eqasim: `model.config` is
`@MODEL_CONFIG_PATH@` (the `--model-config` given to `init`; a relative path is
read from the eqasim folder). Two things to check in that configuration:

- `run:` must include `analysis.synthesis.population`, because the `age`,
  `distance` and `nbr_vehicle` outputs are written by that stage. The official
  `config.yml` only lists `synthesis.output`; `eqasim-sa` refuses to load a
  starter otherwise (`output 'age' is written by eqasim stage ... not in run:`).
- Every parameter you vary must be written in its `config:` section. The
  starters vary only `sampling_rate`, which the official configuration contains.
  To vary `matching_minimum_age`, add it to `config:` first, then add it to
  `parameters` (each starter shows the commented line).

The starters also keep the raw `persons`, `households`, `trips`, `activities`
and `activities_spatial` (`{prefix}activities.gpkg`) files of every run, so a
later analysis does not need new simulations. They have no references, so they
produce no indicator.

### Named matching configurations

Grid can also compare named ordered lists. The eqasim config named by
`model.config` must already contain the active `config.matching_attributes` key
as a list of strings; `init` does not add or modify it. Copy `configs/01-grid.yml`, give the copy a new
`campaign` (its `campaign_root` follows by default), and replace its `parameters` block, for
example:

```yaml
campaign: @PROJECT_NAME@-grid-structured
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
```

Keep the copied Grid's two replication seeds, outputs, execution, and calibration
blocks. This produces 6 positions and 6 expected unique runs. The
labels are readable aliases; execution and `runId` use the resolved ordered
lists. Discrete OAT and canonical Morris reject structured factors. This remains a demonstration,
not a variance-share analysis.

The complete walkthrough is `docs/structured-configurations-tutorial.md` in the
eqasim-sa source repository. It includes model preparation, commands, plan
inspection, interpretation, scientific caveats, and safe recovery.

Every generated configuration starts with exactly three total-variation
signals (the default indicators): age against INSEE, distance against HTS, and number of vehicles against
HTS. eqasim-france still archives `HTS adjust with INSEE population weights`
for compatibility, but that column is a global rescaling of HTS and therefore
has the same normalized TV value; it is intentionally not declared here.

Once written in your configuration, these scalar factors can also be varied:

| Factor | Default | Example domain | Compatible methods |
| --- | ---: | --- | --- |
| `matching_minimum_observations` | `20` | `[5, 20, 50]` | Grid, discrete OAT, Sobol |
| `matching_minimum_age` | `5` | `[0, 5, 12]` | Grid, discrete OAT, Sobol |
| `income_assignation_method` | `uniform` | `[uniform, bhepop2]` | Grid, Sobol |

Adding `matching_minimum_observations` (once written in your configuration) to the
generated Grid gives 12 positions: two rates by three thresholds by two replication seeds.
Use a new `campaign` for that modified design. The starter
indicators do not measure income directly. The `households` output already
contains household income; compute the income measure you need with an
analysis script reading `households` (block `analyses`, see the eqasim-sa
documentation: `docs/analysis-scripts.md` in the eqasim-sa repository) and add an indicator on it before interpreting `income_assignation_method`. Note that `bhepop2` can fall back to
`uniform` for municipalities with invalid Filosofi distributions, and the
current output does not retain the applied method.

