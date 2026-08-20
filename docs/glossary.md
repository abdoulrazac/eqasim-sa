# Glossary

**English** | [Français](../docs-fr/glossary.md)

| Term | Definition |
| --- | --- |
| **Campaign** | Durable collection described by a meta-configuration and stored under a `campaign_root`. |
| **Target** | Grid, Morris, or Sobol scientific campaign whose design is to be run and analyzed, as opposed to resource calibration. |
| **Resource calibration** | Preliminary campaign that measures runtime, durable bytes, and worker cache; it does not tune scientific parameters. |
| **Design / experimental design** | Rules for generating Grid, Morris, or Sobol positions. The sealed file is `plan.parquet`. |
| **Design position** | Statistical row in the plan. Multiple positions may refer to the same run. |
| **`designId`** | Stable 32-character hexadecimal identifier for a design position. |
| **Run** | Unique execution of a resolved eqasim configuration with a seed. |
| **`runId`** | Stable 32-character hexadecimal identifier for the canonical configuration and seed. |
| **Seed** | Integer injected as `random_seed`; depending on the method, it represents a replication or a Sobol factor. |
| **Worker** | Local or SLURM slot that sequentially processes a batch of runs and owns its exclusive cache. |
| **Override** | Infrastructure value applied only at execution time, outside the design and outside the `runId`. |
| **Scalarizer** | Function that reduces an artifact to a scalar indicator; `tv_distance` is the built-in scalarizer. |
| **Backend** | Batch execution mechanism, `local` or `slurm`. |
| **`contextHash`** | Domain-separated SHA-256 of the model commit, manifest, lockfile, and `eqasim-sa` version. |
| **Manifest** | `manifest.json`, a sorted inventory of data files with sizes and SHA-256 hashes, produced by `init`. |
| **Marker** | `runs/<runId>.parquet`, published last and the only recognized proof of completeness. |
| **Archive** | `archives/<runId>/` directory containing the run's durable Parquet artifacts. |
| **Scratch** | Temporary storage for caches and worker attempts; it is disposable and does not prove completeness. |

See [Workflow and results](workflow-and-results.md) for the formats and
[Parameters and methods](parameters-and-methods.md) for their scientific role.
