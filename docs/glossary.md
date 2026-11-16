# Glossary

**English** | [Français](../docs-fr/glossary.md)

| Term | Definition |
| --- | --- |
| **Meta-configuration** | The single YAML file that describes one campaign; omitted keys take default values, and `show-config` prints the complete resolved file. |
| **Campaign** | Durable collection described by a meta-configuration and stored under its `campaign_root` folder, by default `../campaigns/<campaign>`. |
| **Target** | Campaign whose design is to be run and analyzed, as opposed to resource calibration. A generated target is still a demonstration unless it satisfies a scientific contract. |
| **Demonstration campaign** | Small campaign that validates configuration, execution, storage, and analysis plumbing; it cannot support inferential or decision claims. |
| **Scientific campaign** | Campaign with a declared decision setting, uncertainty model, estimand, budget, convergence criteria, and robustness checks. |
| **Screening** | Identifies factors that must remain under consideration; it neither estimates variance shares nor proves that a factor can be fixed. |
| **Factor prioritization (FP)** | Identifies which factor should be learned more accurately first to reduce expected output uncertainty. |
| **Factor fixing (FF)** | Identifies a factor that can be fixed within a declared practical tolerance, including all interactions. |
| **Discrete OAT** | `method: discrete_oat`; adjacent numeric levels and local normalized effects. It is a demonstration design, not canonical Morris. |
| **Canonical Morris** | `method: canonical_morris`; unit-probability trajectories with common Delta and declared transforms. Supported only for scientific screening. |
| **QMC scramble** | One independently randomized Sobol low-discrepancy sequence. Uncertainty evidence requires several independent scrambles, not row bootstrap within one scramble. |
| **Multi-scramble Sobol** | `method: multiscramble_sobol`; scientific FP/FF analysis using independent declared QMC scrambles and between-scramble Student-t uncertainty. |
| **Conditional mean** | Canonical-Morris target `E_seed[Y|X]`, estimated by at least two simulator-seed replications with CRN within each trajectory. |
| **Intrinsic variance / noise floor** | Pointwise unbiased variance across simulator-seed replications, in squared indicator units; it is reported descriptively and does not replace trajectory uncertainty. |
| **Pilot allocation** | Immutable canonical campaign that estimates a replication requirement and recommends a separate production campaign/root. |
| **Finite seed nuisance** | Finite support of the `seed` factor in multi-scramble Sobol, declared in `experiment.seeds` with `experiment.seeds_provenance`. Its indices are diagnostic, non-actionable, and do not identify unrestricted intrinsic noise. |
| **Resource calibration** | Preliminary campaign that measures runtime, durable bytes, and worker cache; it does not tune scientific parameters. |
| **Design / experimental design** | Rules for generating Grid, trajectory, or Sobol positions. The sealed file is `plan.parquet`. |
| **Design position** | Statistical row in the plan. Multiple positions may refer to the same run. |
| **`designId`** | Stable 32-character hexadecimal identifier for a design position. |
| **Run** | Unique execution of a resolved eqasim configuration with a seed. |
| **`runId`** | Stable 32-character hexadecimal identifier for the canonical configuration and seed. |
| **Seed** | Integer injected as `random_seed`, declared in `experiment.seeds`; depending on the method, it represents a replication or a Sobol factor named `seed`. |
| **Worker** | Local or SLURM slot that sequentially processes a batch of runs and owns its exclusive cache. |
| **Machine option** | Infrastructure value under `execution.machine_options`, applied only at execution time, outside the design and outside the `runId`. |
| **Output** | Table written by eqasim for each run (one Parquet file), declared under `outputs` with its class column (`category`), simulated column (`simulated`), and reference columns (`references`). |
| **Indicator** | Number computed from one output of one run, which the analysis studies; `tv_distance`, the total variation distance between simulated and reference distributions, is the built-in indicator. |
| **Backend** | Batch execution mechanism, `local` or `slurm`. |
| **`contextHash`** | Domain-separated SHA-256 of the model commit, manifest, lockfile, and `eqasim-sa` version. |
| **Manifest** | `manifest.json`, a sorted inventory of data files with sizes and SHA-256 hashes, produced by `init`. |
| **Marker** | `runs/<runId>.parquet`, published last and the only recognized proof of completeness. |
| **Archive** | `archives/<runId>/` directory containing the run's durable Parquet outputs. |
| **Scratch** | Temporary storage for caches and worker attempts; it is disposable and does not prove completeness. |

See [Workflow and results](workflow-and-results.md) for the formats and
[Parameters and methods](parameters-and-methods.md) for their scientific role.
