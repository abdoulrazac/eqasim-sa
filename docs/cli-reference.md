# CLI Reference

[English](cli-reference.md) | [Français](../docs-fr/cli-reference.md)

When a public subcommand reaches its application-level processing, it normally
writes a JSON object to standard output, on a single line with sorted keys (the
same formatting for every command). Warnings, notably about Sobol capacity,
are written to standard error. `--help` and omitting the subcommand display text,
not JSON. An `argparse` syntax error displays usage information and returns 2. An
interrupt may terminate with 130 without guaranteeing that a complete JSON object
has been written. `show-config` and `plot` are the exceptions: `show-config`
prints YAML, never JSON; `plot` prints JSON on success but writes its errors as
plain text on standard error.

`eqasim-sa --version` prints the installed version as plain text (for example
`eqasim-sa 0.4.4`) and returns 0.

## `init`

```text
eqasim-sa init --project-name NAME [options]
```

| Option | Required | Default | Contract |
| --- | --- | --- | --- |
| `--project-name NAME` | yes | none | safe segment of 1 to 64 characters |
| `--directory PATH` | no | `./NAME` | project destination |
| `--model-root PATH` | no | `./eqasim-france` | eqasim folder (Git is not required) |
| `--model-config FILE` | no | `config.yml` | eqasim configuration, relative to `--model-root` or absolute; written as `model.config` in the starters and named in the generated README |
| `--data-path PATH` | no | `./eqasim-data` | data tree to hash |
| `--scratch PATH` | no | `<directory>/scratch` | local scratch space, created if absent |
| `--invocation ARG` | no, repeatable | `uv run --locked python -m synpp` | one argument per occurrence, in order |
| `--regenerate-data-manifest` | no | false | rehashes and replaces only `manifest.json` |

JSON statuses: `initialized`, `unchanged`, or `failed`. Fields: `initVersion`,
`projectName`, `manifest`, `readme`, `metaConfigs`, `restored`, `kept`,
`elapsedSeconds`, `error`. `elapsedSeconds` is the duration of the command,
also in a `failed` result. On a rerun, `restored` lists the generated files
that were missing and were recreated (`.gitattributes`, `README.md`, a
starter), and `kept` lists the edited starters and README that were kept as
they are (project-relative paths; both empty for a new project). No starter,
README or `.gitattributes` is ever overwritten.
The command is template-only: hashing the data is not a pipeline execution.

### Rerunning `init` on an existing project

Rerunning `init` with the same options returns `unchanged` without rehashing
the data. Before writing anything, it checks the project:

- at the top: only `manifest.json`, `README.md`, `.gitattributes`, `configs/`,
  `campaigns/`, and the scratch folder when it is inside the project;
- in `configs/`: only the four starters and, optionally, `configs/analyses/`,
  the folder of your analysis scripts (regular files and subfolders, no
  symbolic link; their content is not read);
- the starters: an edited starter is kept as it is and listed in `kept`, as
  long as the values `init` fills in are still those `init` would write:
  `model.root`, `model.config`, `model.data_manifest`, `execution.scratch`,
  `execution.invocation` (absent means the default), and
  `execution.machine_options.data_path`, plus `campaign` and `campaign_root`
  (absent means `../campaigns/<campaign>`). These values are compared as
  written, once the defaults are applied, not as resolved paths: an absolute
  `model.root` that points to the same folder is a change. Adding `analyses`, indicators, or
  changing `workers` or `seeds` is therefore accepted. If one of these values
  differs, the rerun stops with `init inputs changed: <key> in
  configs/<file>`, for example `init inputs changed: model.root in
  configs/01-grid.yml`: this happens when an option (`--model-root`,
  `--model-config`, `--data-path`, `--scratch`, `--invocation`, `--directory`,
  `--project-name`) differs from the first `init`, or when you edited that
  value. A starter that cannot be read as YAML stops the rerun with `init
  cannot parse configs/<file>: <error>`, where `<error>` is the YAML parser's
  error class (`ParserError`, `ScannerError`, ...), the line and column when the
  parser gives them, and its cause (for example `ParserError at line 9, column
  11: expected ',' or ']', but got '<stream end>'`). An edited `README.md` is kept and listed
  in `kept`. A missing starter or README is recreated and listed in
  `restored`;
- in `campaigns/`: only the campaign folders of the four starters, named after
  their `campaign`: `<project>-calibration`, `<project>-grid`,
  `<project>-discrete_oat`, and `<project>-sobol`. Each may hold only what
  `plan`, `run`, `analyze`, and `plot` write there: `plan.parquet`, `runs/`,
  `archives/`, `logs/`, `.eqasim-sa/`, `analysis/`, `analyses-cache/`,
  `figures/`, and the published result files.

Anything else stops the rerun with `foreign content in init project: <entry>`
or `foreign content in init campaign <campaign>: <entry>` (the entry is
relative to that campaign folder): another file or folder in
`configs/` (for example a copied meta-configuration), a campaign folder of
your own (for example `<project>-grid-structured`), or a symbolic link (for
example `foreign content in init project: configs/analyses/link.py`). The
starters are compared before the campaign folders, so a renamed `campaign` is
reported as `init inputs changed: campaign in ...`. Rerunning `init` is never
required to continue: `plan`, `run`, `check`, `estimate`, and `analyze` do not
depend on it.

After a change of the data tree, refresh `manifest.json` with
`--regenerate-data-manifest`. A rerun without this option does not hash the
data again: it returns `unchanged` even if the data changed, so only
`--regenerate-data-manifest` detects a data change. With this option, the project must pass the same checks (kept
starters included); then only `manifest.json` is replaced and the status is
`initialized`. Plans and results are not rewritten. If the data changed, a
campaign planned before refuses to resume: `run` stops with `data manifest
changed since plan (dataManifestSha256)`.

## `show-config`

```text
eqasim-sa show-config PATH
```

Prints the complete meta-configuration, with every default value applied (see
[Defaults and `show-config`](yaml-reference.md#defaults-and-show-config)). The
file is validated as for `plan` (eqasim folder and configuration found, factors
present in the model configuration), but the scratch and data directories need not exist and
no `execution` block is required.
`execution.python` is written when it derives from `execution.invocation` (see
the table of defaults).

On success, the resolved YAML is written to standard output and the exit code is
0. Paths are printed as written in the file, so the output is itself a valid
meta-configuration. On an invalid file, the error message alone is written to
standard error, nothing is written to standard output, and the exit code is 1.
Unlike the other subcommands, there is no JSON envelope.

## `plan`

```text
eqasim-sa plan --meta-config PATH
```

Seals the deterministic design in `<campaign_root>/plan.parquet`. An identical
rerun returns `unchanged`; a different plan under the same root fails. JSON
fields: `status` (`created`, `unchanged`, or `failed`), `path`,
`designPositions`, `uniqueRuns`, `contextHash`, `error`. In a `failed` result,
`path` and `contextHash` are `null` and both counts are 0.

## `check`

```text
eqasim-sa check --meta-config PATH \
  [--calibration-meta-config PATH ...] [--smoke]
```

`--calibration-meta-config` is repeatable and is appended to the paths from the
YAML `calibration` block; duplicates are removed while preserving order.

Without `--smoke`, the checks are, in this order:

| Check | Verification | Statuses |
| --- | --- | --- |
| `metaConfig` | YAML, eqasim code fingerprint and context, factors, declared outputs, and paths | `passed`, `failed` |
| `sealedPlan` | equality of the plan with the recomputed design | `passed`, `failed` |
| `uniqueConfigSeedPairs` | no Grid duplicates; sealed validation for discrete OAT, canonical Morris, and Sobol | `passed`, `failed` |
| `eqasimEnvironment` | when `execution.invocation` ends with `-m synpp`, the invocation with those two elements replaced by `-c "import synpp"` must exit 0; otherwise the probe is skipped, with `note: synpp probe skipped: invocation does not end with -m synpp` | `passed`, `skipped`, `failed` |
| `backend` | the `slurm` backend runs only on Linux | `passed`, `failed` |
| `path_length` | estimated longest path below `execution.scratch`: eqasim-sa's worker layout, the `check --smoke` layout, and 240 characters for eqasim's own cache. The limit is 260 on Windows (passes above it when Windows long paths are enabled, otherwise fails and says how short `execution.scratch` must be), 1024 on macOS and 4096 on Linux (a `warning` above it). The estimate is in `note` when the item passes, in `error` otherwise | `passed`, `warning`, `failed` |
| `same_machine` | the completed runs of the campaign were executed on this machine with the same executable, environment variables, model configuration, and eqasim-sa installation (see [Troubleshooting](troubleshooting.md#campaign-runs-were-executed-on-another-machine)). `skipped` before any completed run, for an empty plan, and for the `slurm` backend (compared on the compute node); `note` says which | `passed`, `skipped`, `failed` |
| `diskSpace` | durable and scratch space based on available observations | `passed`, `failed` |

`backend`, `path_length`, and `same_machine` appear only when the
meta-configuration loads and has an `execution` block; `same_machine` also
needs a valid `sealedPlan`. `--smoke` adds a ninth item, `smoke` (`passed` or
`failed`).

Each item is `{name, status, error}`, plus `note` for some. `error` is set only
when the status is `failed` or `warning`, and is `null` otherwise; `note`
carries information for `passed` and `skipped` items. Only `failed` makes the
overall status `invalid`; `warning` and `skipped` keep it `valid`.

The static check has an overall budget of 55 seconds, and the environment probe
uses at most 20 seconds of that budget.

`--smoke` is not static. If the preceding checks pass, it executes two real,
temporary eqasim runs with the first two distinct seeds and
`sampling_rate: 0.001`, validates their markers and that every declared output
file was archived, then deletes
the temporary directory. That directory is `<execution.scratch>/.smoke-<8 hex>/`,
so the `path_length` estimate covers the smoke runs too. It requires
`sampling_rate` and two distinct explicit seeds (otherwise: `smoke requires
sampling_rate and two distinct explicit seeds`). Its starting configuration is
that of the first run of the plan, with `execution.machine_options` applied as
for `run`: `execution.machine_options.data_path` is therefore the data folder
the smoke runs read. The indicators that read an
[analysis output](analysis-scripts.md) are skipped, since `check` never runs
analysis scripts.

JSON fields: `status` (`valid` or `invalid`), `checkVersion`,
`elapsedSeconds`, `checks`, the disk measurements below, then `error`.
`elapsedSeconds` is the duration of the whole command, smoke runs included.

| Field | Meaning |
| --- | --- |
| `estimatedDiskBytes` | durable size of the whole campaign, as `estimatedDiskBytes` of `estimate`: size of `plan.parquet` plus the number of unique runs of the plan, completed ones included, times the mean durable size of an observed run |
| `requiredDiskBytes` | what is left to write: `estimatedDiskBytes` minus what the campaign already holds (its `plan.parquet` and, for each completed run, its marker and archived files), at least 0. It is therefore smaller than `estimatedDiskBytes` as soon as the plan is written |
| `freeDiskBytes` | free space on the file system of the campaign folder (or of its nearest existing parent) |
| `missingDiskBytes` | `requiredDiskBytes` minus `freeDiskBytes`, at least 0 |
| `estimatedScratchBytes` | `execution.workers` times the largest observed worker cache |
| `freeScratchBytes` | free space on the file system of `execution.scratch` |
| `missingScratchBytes` | `estimatedScratchBytes` minus the free scratch space, at least 0; when the scratch and the campaign share a file system, the free space left after `requiredDiskBytes` is used |

All seven are `null` when they cannot be computed, for example before any
completed run (`diskSpace` then fails with `no completed calibration run yet:
run ... first`, see [Troubleshooting](troubleshooting.md#no-completed-calibration-run-yet-run--first)). Before `plan`, `sealedPlan` and `diskSpace` fail with
`campaign <campaign> has no plan yet: run plan first`; `diskSpace` gives the
same message, naming the calibration campaign, when a calibration
meta-configuration has not been planned. `estimate` stops with the same
message.

## `estimate`

```text
eqasim-sa estimate --meta-config PATH \
  [--calibration-meta-config PATH ...]
```

Requires a target plan, an `execution` block, and at least one completed run from
the target or calibration campaigns. For each observation, `duration` and
`scratchBytes` come from the marker, while durable bytes are measured from
the marker and every file in the archive. The calculation uses mean
duration and durable size, and the maximum cache size per worker.

JSON fields: `status`, `estimateVersion`, `method`, `formula`,
`formulaInputs`, `designPositions`, `runs`, `calibrationRuns`,
`meanDurationSeconds`, `meanDurableBytesPerRun`, `estimatedComputeHours`,
`estimatedDiskBytes`, `workers`, `observedScratchBytesPerWorker`,
`estimatedScratchBytes`, `error`.

`calibrationRuns` is the number of observations behind the means: every
completed run of the calibration campaigns **and of the target campaign
itself** (each campaign folder counted once). It is not only the runs of the
calibration campaigns: after some target runs complete, it grows and the means
include them. `runs` is the number of unique runs of the target plan,
completed ones included; `estimatedComputeHours` (`runs` times the mean
duration) and `estimatedDiskBytes` (size of `plan.parquet` plus `runs` times
the mean durable size) cover the whole campaign, not only what remains. For
the space still needed, see `requiredDiskBytes` of `check`.

`formula` gives the number of design positions for the method, and
`formulaInputs` its values; `designPositions` equals the formula evaluated on
them:

| Method | `formula` | `formulaInputs` |
| --- | --- | --- |
| Grid | `R*prod(L)` | `R`: number of seeds; `L`: number of values of each parameter, e.g. `{"R": 2, "L": {"sampling_rate": 2}}` |
| Discrete OAT, canonical Morris | `T(D+1)R` | `D`: parameters; `T`: trajectories; `R`: seeds |
| Sobol | `N(2D+2)` | `D`: parameters plus the seed; `N`: base sample size |
| Multi-scramble Sobol | `S*N(2D+2)` | `D`: parameters plus the seed; `N`: largest base sample size; `S`: scramble seeds |

Both are `null` only in a `failed` result. A `failed` result computes nothing:
every field other than `status`, `estimateVersion`, and `error` is `null`
(never `0`).

## `run`

```text
eqasim-sa run --meta-config PATH
```

Requires `execution` and a sealed plan. Executes only `runId` values without a
valid marker. A second `run` started on the same campaign while the first is
active does not fail: it waits for the worker slots and runs held by the first,
and never executes a `runId` twice (a run completed meanwhile counts as
`skipped`). JSON fields: `status` (`completed`, or `failed` when at least one
run failed), `total`, `completed`, `skipped`, `failed`, `failures` (one
`{runId, error}` per failed run), `error`. When everything is already
complete, `total` is 0. The exit code is 1 whenever `status` is `failed`.

## `analyze`

```text
eqasim-sa analyze --meta-config PATH [--descriptive-only]
```

Recomputes indicators from the archived output files and atomically publishes
analysis outputs. The output columns (`category`, `simulated`, `references`)
and the indicators are read from the current meta-configuration, so they can be
corrected after the runs. The [analysis scripts](analysis-scripts.md) read by
indicators run here, on copies of the archived files, with a cache under
`analyses-cache/`. JSON fields: `status`, `path`, `runs`, `indicators`, `values`,
`completionRate`, `expected`, `completed`, `missing`, `failed`,
`dispersionPath`, `sensitivityMethod`, `sensitivityPath`, `convergencePath`,
`rankReversalsPath`, `byScramblePath`, `diagnosticsPath`, `noiseFloorPath`,
`replicationAllocationPath`, `outputSummaryPath`, `convergenceEvidencePath`,
`governedReportPath`, `inferenceStatus`, `analyses`, `warnings`, `error`.
`warnings` lists altered analysis cache entries that were removed and
recomputed (empty otherwise). `analyses` maps each analysis read by an
indicator to `{"computed": n, "cached": m}`: the number of runs whose result
was computed by running the script, and the number read back from
`analyses-cache/`. After editing one script, only that analysis has
`computed` results. It is `{}` when no indicator reads an analysis output.
In a failed result, `analyses` is `{}` and `warnings` is `[]`.
`rankReversalsPath`, `noiseFloorPath`, and `replicationAllocationPath` are
specific to `canonical_morris` (the last one to a pilot; it is `null` in
production); `byScramblePath` and `diagnosticsPath` are specific to
`multiscramble_sobol`; `convergencePath` is used by both. A path that does not
apply is `null`.

`inferenceStatus` is `incomplete_noninferential` when runs are missing or
failed, `demonstration_noninferential` for Grid, discrete OAT, and Sobol,
`pilot_only` for a canonical Morris pilot, and `conditional` or `unstable`
(convergence criteria met or not) for canonical Morris production and
multi-scramble Sobol.

`--descriptive-only` ignores `analysis.completeness_threshold` and publishes
only descriptive outputs of the completed runs, even an incomplete or empty
campaign: `indicators.parquet`, `output-summary.parquet`,
`governed-report.json`, and `replication-dispersion.parquet` when seeds are
replications. No sensitivity table is published (`sensitivityMethod` is
`null`), and `inferenceStatus` is `descriptive_noninferential`, or
`incomplete_noninferential` when runs are missing.

## `plot`

```text
eqasim-sa plot --meta-config PATH [--figure NAME ...]
```

Draws the figures valid for the published analysis generation into
`figures/<generationId>/` (PNG files and `figures.json`), after verifying the
sha256 of every published output it reads. It requires a previous `analyze` and writes
nothing under `analysis/`. `--figure` is repeatable and restricts the drawing to
the named figures; a figure that is not valid for the campaign's method and
published tables is an error that lists the valid figures. The directory of the
generation is rebuilt as a whole on each run. See
[Figures](workflow-and-results.md#figures) for the figure names and their
validity. Stdout is a JSON object with `status`, `generationId`, `directory`,
`figures` and `skipped`. On error, the message goes to stderr, nothing is
written to stdout, and the exit code is 1.

## Exit Codes

| Code | Meaning |
| ---: | --- |
| 0 | success, help without a command, configuration shown, unchanged plan, or run already complete |
| 1 | failed validation (including an invalid file for `show-config`), application failure, at least one failed run, or a Python other than 3.13 |
| 2 | invalid CLI arguments reported by `argparse` |
| 130 | keyboard interrupt handled at the application level |

Application errors are normally encoded in JSON; `show-config` and `plot` write
them as plain text on standard error. `init` and `check` redact
absolute paths from their messages; this guarantee is not applied uniformly to
the other commands.
