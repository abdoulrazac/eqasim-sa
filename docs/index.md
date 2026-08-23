# eqasim-sa documentation

**English** | [Français](../docs-fr/index.md)

This documentation describes the behavior observed in `src/` and `tests/`.
The Python parser is authoritative: the project does not provide a JSON Schema.

## User documentation

1. [Getting started](getting-started.md): installation, `init`, and the first campaign.
2. [Named structured configurations tutorial](structured-configurations-tutorial.md): compare ordered `matching_attributes` lists end to end.
3. [YAML reference](yaml-reference.md): the complete meta-configuration contract.
4. [Parameters and methods](parameters-and-methods.md): Grid, discrete OAT, canonical Morris, Sobol, and seeds.
5. [Workflow and results](workflow-and-results.md): identities, storage, and resumption.
6. [CLI reference](cli-reference.md): commands, JSON, and exit codes.
7. [Troubleshooting](troubleshooting.md): common errors and safe remedies.
8. [Glossary](glossary.md): precise project terminology.
9. [Release compatibility](compatibility.md): schema, method, inference, refusal, and release matrices.

## Method chooser

| Guide | Question | Status | Recommended use |
| --- | --- | --- | --- |
| [Grid](what-when-how-grid.md) | What happens at every declared combination? | Demonstration | Small readable comparisons and named structured configurations |
| [Historical Morris](what-when-how-morris.md) | What did an old adjacent-level campaign record? | Legacy demonstration | Read existing provenance only; never start a new campaign |
| [Discrete OAT](what-when-how-discrete_oat.md) | Which local adjacent numeric changes need attention? | Demonstration | Local numeric screening for more study |
| [Canonical Morris](what-when-how-canonical_morris.md) | Which factors must remain under consideration? | Scientific screening | Study-specific pilot, then separate production |
| [Historical Sobol](what-when-how-sobol.md) | Does the single-scramble Sobol plumbing work? | Legacy noninferential demonstration | Run the generated starter or read legacy provenance |
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
| Validate statically | `check` | plan; for a target, referenced calibration with at least one complete run | checks without a target run |
| Run an actual test | `check --smoke` | `check` conditions | two actual temporary runs |
| Estimate a target | `estimate` | at least one complete calibration run | JSON runtime/disk/scratch estimate |
| Run | `run` | sealed plan, `execution` block | missing archives and markers |
| Recompute the analysis | `analyze` | sufficient completeness | indicator and sensitivity Parquet files |

## Methods

| Method | Role of the seed | Positions | Method-specific output |
| --- | --- | --- | --- |
| Grid | replication | product of domain sizes × seeds | `replication-dispersion.parquet` |
| Historical `morris` | replication | `T(D+1)R` | `morris-ranking.parquet` |
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
Historical `morris` remains readable but is not recommended.

`init` prepares the files and fingerprints. Only the calibration `run` provides
the resource observations used by `check` and `estimate`.

## Rules to remember

- Run calibration before `check` or `estimate` for a target that references it.
- Never edit `plan.parquet` or `runs/<runId>.parquet` manually.
- Keep the model repository clean and its configuration tracked by Git.
- Use a new `campaign_root` when the context or design changes.
- Treat scratch as disposable, never as proof of completeness.
