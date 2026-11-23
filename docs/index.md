# eqasim-sa documentation

**English** | [Français](../docs-fr/index.md)

This documentation describes the behavior observed in `src/` and `tests/`.
The Python parser is authoritative: the project does not provide a JSON Schema.

## User documentation

1. [Getting started](getting-started.md): installation, `init`, and the first campaign.
2. [Named structured configurations tutorial](structured-configurations-tutorial.md): compare ordered `matching_attributes` lists end to end.
3. [YAML reference](yaml-reference.md): the complete meta-configuration contract.
4. [Writing an analysis script](analysis-scripts.md): add quantities computed from the archived files, even after the runs.
5. [Parameters and methods](parameters-and-methods.md): Grid, discrete OAT, canonical Morris, Sobol, and seeds.
6. [Workflow and results](workflow-and-results.md): identities, storage, resumption, and figures.
7. [CLI reference](cli-reference.md): commands, JSON, and exit codes.
8. [Troubleshooting](troubleshooting.md): common errors and safe remedies.
9. [Glossary](glossary.md): precise project terminology.
10. [Release compatibility](compatibility.md): schema, method, inference, refusal, and release matrices.

## Method chooser

If you are new to sensitivity analysis, start with the
[Grid worked example](what-when-how-grid.md). It follows a question about
`sampling_rate` from parameter and indicator declarations through the generated
outputs and their interpretation. Continue with discrete OAT for local changes,
then use canonical Morris and multi-scramble Sobol only for a declared scientific
campaign.

| Guide | Question | Status | Recommended use |
| --- | --- | --- | --- |
| [Grid](what-when-how-grid.md) | What happens at every declared combination? | Demonstration | Small readable comparisons and named structured configurations |
| [Discrete OAT](what-when-how-discrete_oat.md) | Which local adjacent numeric changes need attention? | Demonstration | Local descriptive triage for a later study |
| [Canonical Morris](what-when-how-canonical_morris.md) | Which factors must remain under consideration? | Scientific screening | Study-specific pilot, then separate production |
| [Demonstration Sobol](what-when-how-sobol.md) | Does the single-scramble Sobol plumbing work? | Noninferential demonstration | Run the generated starter |
| [Multi-scramble Sobol](what-when-how-multiscramble_sobol.md) | What should be learned first, or what may be fixed? | Scientific FP or FF | Study-specific factor prioritization or factor fixing |

## Contributor documentation

- [Development guide](development-guide.md): architecture, dependencies, tests, and distribution.
- [Generated templates](../src/eqasim_sa/templates/): normative starter README and meta-configurations.

Return to the [home page](../README.md).

## Quick reference matrix

| Need | Command | Durable prerequisite | Primary effect |
| --- | --- | --- | --- |
| Generate a project | `init` | model, data | prepares files and hashes data without running the pipeline |
| Seal a design | `plan` | valid meta-configuration | immutable `plan.parquet` |
| Validate statically | `check` | plan, and at least one completed run of the referenced calibration (of the campaign itself when none is referenced, as for `00-calibration.yml`) | checks without a target run |
| Run an actual test | `check --smoke` | `check` conditions | two actual temporary runs |
| Estimate a target | `estimate` | at least one complete calibration run | JSON runtime/disk/scratch estimate |
| Run | `run` | sealed plan, `execution` block | missing archives and markers |
| Recompute the analysis | `analyze` | sufficient completeness (none with `--descriptive-only`) | indicator and sensitivity Parquet files |
| Draw figures | `plot` | published analysis (`analyze` done), campaign meta-configuration and sealed plan | PNG figures valid for the method, plus `figures.json`, under `figures/<generationId>/` |

## Methods

| Method | Role of the seed | Positions | Method-specific output |
| --- | --- | --- | --- |
| Grid | replication | product of domain sizes × seeds | `replication-dispersion.parquet` |
| Discrete OAT | replication | `T(D+1)R` | `discrete-oat-effects.parquet` |
| Canonical Morris | derived trajectory-block replication | `T(D+1)R` | ranking, convergence, and rank-reversal Parquet files |
| Sobol | factor | `N(2D+2)` | `sobol-indices.parquet` |
| Multi-scramble Sobol | factor | `S*N(2D+2)` | four `multiscramble-sobol-*.parquet` files |

`D` includes the seed for Sobol. The number of unique runs may be lower than the
number of positions because multiple statistical positions may refer to the
same resolved configuration.

`init` generates Grid, discrete OAT, and Sobol demonstration starters. It does
not generate canonical Morris, whose scientific screening contract must be
authored for the study. It also generates no multi-scramble Sobol starter; its
scientific FP/FF contract and independent scramble seeds must be study-specific.

`init` prepares the files and fingerprints. Only the calibration `run` provides
the resource observations used by `check` and `estimate`.

## Rules to remember

- Run calibration before `check` or `estimate` for a target that references it.
- Never edit `plan.parquet` or `runs/<runId>.parquet` manually.
- Do not change the eqasim code or configuration during a campaign: both are part of its identity. Once the runs are finished, `analyze` still works after such changes.
- Declare under `outputs`, before planning, every file you may want to analyze later: analysis scripts read only archived files.
- Add `analysis.synthesis.population` to `run:` in your eqasim configuration before using the `age`, `distance` or `nbr_vehicle` outputs.
- Use a new `campaign` name (and so a new campaign folder) when the context or design changes.
- Treat scratch as disposable, never as proof of completeness.
