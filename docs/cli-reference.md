# CLI Reference

[English](cli-reference.md) | [Français](../docs-fr/cli-reference.md)

When a public subcommand reaches its application-level processing, it normally
writes a JSON object to standard output. Warnings, notably about Sobol capacity,
are written to standard error. `--help` and omitting the subcommand display text,
not JSON. An `argparse` syntax error displays usage information and returns 2. An
interrupt may terminate with 130 without guaranteeing that a complete JSON object
has been written. `show-config` is the exception: it prints YAML, never JSON.

## `init`

```text
eqasim-sa init --project-name NAME [options]
```

| Option | Required | Default | Contract |
| --- | --- | --- | --- |
| `--project-name NAME` | yes | none | safe segment of 1 to 64 characters |
| `--directory PATH` | no | `./NAME` | project destination |
| `--model-root PATH` | no | `./eqasim-france` | eqasim folder (Git is not required) |
| `--model-config FILE` | no | `config.yml` | eqasim configuration, relative to `--model-root` or absolute; written as `model.config` in the starters |
| `--data-path PATH` | no | `./eqasim-data` | data tree to hash |
| `--scratch PATH` | no | `<directory>/scratch` | local scratch space, created if absent |
| `--invocation ARG` | no, repeatable | `uv run --locked python -m synpp` | one argument per occurrence, in order |
| `--regenerate-data-manifest` | no | false | rehashes and replaces only `manifest.json` |

JSON statuses: `initialized`, `unchanged`, or `failed`. Fields: `initVersion`,
`projectName`, `manifest`, `readme`, `metaConfigs`, `restored`, `elapsedSeconds`,
`error`. `restored` lists generated files that were missing from an existing
project and were recreated (currently only `.gitattributes`); an existing
`.gitattributes` is never overwritten.
The command is template-only: hashing the data is not a pipeline execution. With
`--regenerate-data-manifest`, an existing project must still pass the strict
checks on its static files and durable state; only the manifest is replaced.
Plans and results are not rewritten.

## `show-config`

```text
eqasim-sa show-config PATH
```

Prints the complete meta-configuration, with every default value applied (see
[Defaults and `show-config`](yaml-reference.md#defaults-and-show-config)). The
file is validated as for `plan` (eqasim folder and configuration found, factors
present in the model configuration), but the scratch and data directories need not exist and
no `execution` block is required.

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
fields: `status`, `path`, `designPositions`, `uniqueRuns`, `contextHash`,
`error`.

## `check`

```text
eqasim-sa check --meta-config PATH \
  [--calibration-meta-config PATH ...] [--smoke]
```

`--calibration-meta-config` is repeatable and is appended to the paths from the
YAML `calibration` block; duplicates are removed while preserving order.

Without `--smoke`, the checks are:

| Check | Verification |
| --- | --- |
| `metaConfig` | YAML, eqasim code fingerprint and context, factors, declared outputs, and paths |
| `sealedPlan` | equality of the plan with the recomputed design |
| `uniqueConfigSeedPairs` | no Grid duplicates; sealed validation for discrete OAT, canonical Morris, and Sobol |
| `eqasimEnvironment` | when `execution.invocation` ends with `-m synpp`, the invocation with those two elements replaced by `-c "import synpp"` must exit 0; otherwise the probe is skipped: the check has `status: skipped` (which keeps the overall status `valid`) and carries `note: synpp probe skipped: invocation does not end with -m synpp` |
| `diskSpace` | durable and scratch space based on available observations |

The static check has an overall budget of 55 seconds, and the environment probe
uses at most 20 seconds of that budget.

`--smoke` is not static. If the preceding checks pass, it executes two real,
temporary eqasim runs with the first two distinct seeds and
`sampling_rate: 0.001`, validates their markers and that every declared output
file was archived, then deletes
the temporary directory. That directory is `<execution.scratch>/.smoke-<8 hex>/`,
so the `path_length` estimate covers the smoke runs too. It requires `sampling_rate` and two explicit seeds. It
does not apply `execution.machine_options`: its starting configuration is the
sealed model YAML. Consequently, `execution.machine_options.data_path` is
ignored by the smoke test, and the model's `data_path` must be usable from the machine running
the test.

JSON fields: `status` (`valid` or `invalid`), `checkVersion`,
`elapsedSeconds`, `checks`, the `estimated/required/free/missing` measurements
for durable and scratch disk space, then `error`.

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

## `run`

```text
eqasim-sa run --meta-config PATH
```

Requires `execution` and a sealed plan. Executes only `runId` values without a
valid marker. A campaign cannot have two concurrent `run` invocations. JSON
fields: `status`, `total`, `completed`, `skipped`, `failed`, `failures`,
`error`. When everything is already complete, `total` is 0.

## `analyze`

```text
eqasim-sa analyze --meta-config PATH
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
`governedReportPath`, `inferenceStatus`, `warnings`, `error`.
`warnings` lists altered analysis cache entries that were removed and
recomputed (empty otherwise).
`rankReversalsPath` is specific to `canonical_morris`; the two scramble paths are
specific to `multiscramble_sobol`.
scientific analysis accepts `multiscramble_sobol` after reconstructing the
current meta-configuration and sealed plan. Canonical production has a null
`replicationAllocationPath`; pilot and production report `pilot_only` and
`unstable` or `conditional`, respectively. Analyze JSON adds optional
`byScramblePath` and `diagnosticsPath`; `convergencePath` is reused. New generated
trajectory demonstrations use `discrete_oat`.

## Internal Commands

These interfaces are used for tests and for executing an isolated run. They are
not part of the normal user workflow.

### Internal: `fake-run`

```text
eqasim-sa fake-run --campaign-root PATH --campaign NAME --config JSON \
  --seed INT --expected-context-hash HASH --current-context-hash HASH \
  --workdir PATH
```

`--config` identifies a JSON file. Response: `runId`, `status`, `error`.

### Internal: `eqasim-run`

```text
eqasim-sa eqasim-run --campaign-root PATH --campaign NAME \
  --config-yaml PATH --eqasim-root PATH --invocation-json JSON \
  --scratch PATH --seed INT --expected-context-hash HASH \
  --current-context-hash HASH --outputs-json JSON
```

`--invocation-json` must be a JSON array of strings. `--outputs-json` is a JSON
object mapping each declared output name to exactly
`{"file": ..., "separator": ...}` (the `file` may use `{prefix}`), for example
`{"age": {"file": "analysis_population/{prefix}age.csv", "separator": ","}}`;
the run archives exactly these files. Response: `runId`, `status`, `error`.

## Exit Codes

| Code | Meaning |
| ---: | --- |
| 0 | success, help without a command, configuration shown, unchanged plan, or run already complete |
| 1 | failed validation (including an invalid file for `show-config`), application failure, or at least one failed run |
| 2 | invalid CLI arguments reported by `argparse` |
| 130 | keyboard interrupt handled at the application level |

Application errors are normally encoded in JSON; `show-config` writes them as
plain text on standard error. `init` and `check` redact
absolute paths from their messages; this guarantee is not applied uniformly to
the other commands.
