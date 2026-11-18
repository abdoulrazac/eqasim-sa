# Troubleshooting

[Français](../docs-fr/troubleshooting.md)

## `No such file or directory` for `model.config`

Cause: a relative `model.config` is read from the eqasim folder (`model.root`),
not from the directory of the meta-configuration. A path such as
`../../eqasim-france/config.yml`, written relative to `configs/`, therefore
points to the wrong place. Remedy: write the path relative to `model.root`
(`config: config.yml`, or `config: my_config.yml` for a copy next to it), or
an absolute path. The message shows the full path that was tried.

## `no eqasim source files found under ...`

Cause: `model.root` contains no `*.py`, `*.java`, `pom.xml`, `pyproject.toml`
or `uv.lock` outside the ignored folders, so it is probably not the eqasim
folder. Remedy: point `model.root` at the folder that contains eqasim's code and
its `config.yml`.

## `eqasim source changed after context sealing`

Cause: an eqasim code file under `model.root` (`*.py`, `*.java`, `pom.xml`,
`pyproject.toml`, `uv.lock`) was added, removed, or edited after the campaign
was planned. The code is part of the campaign identity, and every run checks its
fingerprint before starting. Remedy: restore the code as it was when the plan was
sealed, or plan a new campaign with the new code. Files written in the eqasim
`working_directory` and `output_path`, and in virtual or conda environments,
are ignored. If another tool writes code files inside the eqasim folder, move
its output elsewhere.

## Campaign identity changes after running eqasim with another configuration

Cause: only the `working_directory` and `output_path` of the configuration named
by `model.config` are left out of the eqasim code fingerprint. A synpp cache
written inside the eqasim folder by another configuration (for example a copy
with a different `working_directory`) is fingerprinted like the code: its
`matsim.*` stages clone Java projects with `*.java` and `pom.xml` files. The
campaign identity then changes, so `plan` reports a different plan and a
running campaign stops with `eqasim source changed after context sealing`.
Remedy: keep `working_directory` outside the eqasim folder, or use the same
`working_directory` in every configuration you run from that folder.

## `eqasim source contains a file name that is not valid UTF-8: ...`

Cause: a code file under `model.root` has a name whose bytes are not valid
UTF-8 (possible on Linux), so its path cannot enter the fingerprint. Remedy:
rename or remove that file; the message shows the name with the invalid bytes
escaped.

## `eqasim source changed since plan (eqasimSourceSha256)` and similar

Cause: when a campaign is resumed or read (`run`, `check`, `estimate`, `analyze`), the plan recomputed from the
current inputs is compared with the sealed `plan.parquet`. The message names
what differs: `eqasim source` (`eqasimSourceSha256`), `data manifest`
(`dataManifestSha256`), `lockfile` (`lockfileSha256`), `eqasim-sa version`
(`eqasimSaVersion`), or an output (`output 'age' changed since plan: file`,
`... separator`, `output '...' added since plan`, `output '...' removed since
plan`). Remedy: restore what changed, or plan a new campaign.

## `model config changed after context sealing`

Cause: the eqasim configuration named by `model.config` was edited during the
campaign. Remedy: restore it, or plan a new campaign with the edited file.

## `output '...' is written by eqasim stage '...', which is not in run: of ...`

Cause: a known output (for example `age`, `distance`, or `nbr_vehicle`) keeps
its default file, but the eqasim stage that writes it is not in the `run:` list
of your eqasim configuration. eqasim-sa runs `run:` exactly as written and does
not add stages. Remedy: add the named stage, usually
`analysis.synthesis.population`, to `run:` in your copy of the configuration,
before planning. See [What eqasim runs](yaml-reference.md#what-eqasim-runs).

## `output '...' is not a known eqasim output; declare its file`

Cause: an output name outside the twelve known eqasim outputs has no `file`.
Remedy: add `file`, relative to eqasim's output folder, for example
`file: "analysis_population/{prefix}summary.csv"`, and `separator` if it is not
a comma. See [Known outputs](yaml-reference.md#known-outputs-and-their-files).

## `eqasim did not write declared output '...' (expected ...)`

Cause: a run finished but the declared file is not in eqasim's output folder.
Typical reasons: the stage that writes it is not in `run:`, the `file` path is
misspelled, or `{prefix}` does not match the `output_prefix` of your eqasim
configuration. Remedy: read `logs/<runId>.log`, correct the eqasim
configuration or the `file` declaration (a new campaign is needed if the plan
was already sealed), and run again.

## `share category must be a quoted string` or `where values must be quoted strings`

Cause: YAML reads `category: 0`, `category: True`, or `zone: 44` as a number or
a boolean, while class labels and table cells are compared as text. Remedy:
write them between quotes: `category: "0"`, `category: "True"`,
`where: {zone: "44"}`.

## `artifact '...' is missing declared column '...'` or `column '...' must be numeric`

Cause: a `category`, `simulated`, reference, `value` column, or `where` column
does not exactly match the CSV header, or a column used as a number contains
text. Remedy: print the first line of the archived file
(`head -n 1 archives/<runId>/age.csv`), correct the column names under
`outputs` or `indicators`, and rerun `analyze`. These keys are not sealed in the
plan, so no run needs to be repeated.

## `plan differs; choose a new campaign_root`

Cause: the design, parameters, context, package version, or indicator
requirements differ under a root that has already been sealed. Remedy: give the
campaign a new `campaign` name (with the default `campaign_root`, this also gives
it a new folder), or set a new `campaign_root`. Do not replace `plan.parquet`.
Renaming a campaign also changes its default `design_seed`.

## `campaign_root contains durable state without a plan`

Cause: residual state or unrelated content exists in the root. Remedy: identify
its provenance. For a new study, choose an empty root. Do not create a plan over
existing state.

## `provide a calibration meta-config with a complete run`

Cause: no complete marker is available in the target or calibration campaigns.
Remedy: run `plan`, then `run` for the calibration. Then run `plan`, `check`, and
`estimate` for the target.

## `unknown factor` or `reserved or infrastructure factor`

Cause: the name is absent from the eqasim `config:` block, is a dedicated option,
belongs to infrastructure, or is a reserved column. Remedy: use an active
scientific key; place only permitted environment options in
`execution.machine_options`.

## `... keys must be among ...`

Cause: a key is misspelled, misplaced, or does not belong to the chosen method
(for example `bootstrap_resamples` in a Grid `analysis` block). The message lists
the keys allowed at that level; `missing required` names a required key that is
absent and `unknown` names the key that is refused. Remedy: correct the key as
described in the [YAML reference](yaml-reference.md). Once the file loads,
`"${SA[@]}" show-config FILE` prints the complete configuration that eqasim-sa
uses.

## `declare simulation seeds in experiment.seeds, not parameters.seed`

Cause: a parameter named `seed` is declared under `parameters`. Remedy: move the
values to `experiment.seeds`. For Sobol and multi-scramble Sobol, they become
the `seed` factor; for the other methods, they are replications.

## `factor ... requires a distribution under a scientific charter`

Cause: a parameter uses the short form `name: [a, b]` (or `values` alone) in a
campaign with `charter.status: scientific`. Remedy: use the long form with
`values` and `distribution`, as described in the
[YAML reference](yaml-reference.md#under-a-scientific-charter). For
multi-scramble Sobol, the seed factor also needs `experiment.seeds_provenance`.

## Type error in `parameters`

Common causes: a boolean, `null`, mixed levels, a non-finite float, or an
incorrectly ordered discrete-OAT domain. Remedy: keep the type
homogeneous and numbers finite. Where a float is required, such as a charter
`threshold` or `reserve_fraction`, write a decimal point (`1.0`, not `1`);
`analysis.completeness_threshold` alone also accepts `0` and `1`.

## Named structured factor errors

- `factor 'matching_attributes' must contain exactly ...`: a demonstration
  declaration has exactly `configurations`; a scientific declaration has exactly
  `configurations` and `distribution`. Do not combine `values` with
  `configurations`.
- `unknown factor` or `model config value must be a list of strings`: the sealed
  model YAML must already contain active `config.matching_attributes` as a list
  of strings. Write it in your eqasim configuration before planning; eqasim-sa
  does not create or coerce the key.
- `must be a non-empty ordered list...`, `distinct payloads`, or `payload
  aliases`: every configuration payload must be a non-empty list of unique,
  non-empty strings, and no two labels may resolve to the same ordered list.
  Labels must also be non-empty strings without NUL.
- `does not support structured factors`: `discrete_oat` and
  `canonical_morris` both refuse named structured payloads. Use Grid for a
  demonstration, or a fully chartered `multiscramble_sobol` design where equal
  label probabilities are scientifically justified.
- `structured plan metadata ...`, `mismatch`, or `unknown label`: schema-v3
  `structuredFactorsVersion` and canonical `structuredFactors` metadata are
  sealed together with labels and run identities. Preserve the damaged state for
  diagnosis and create a plan under a new root; never edit `plan.parquet`.
- No `--matching_attributes` appears in the model command: this is intentional.
  Composite values are written into the per-run YAML snapshot and are not passed
  through synpp's scalar CLI transport. Inspect the failed attempt's `config.yml`
  rather than adding a manual list-valued CLI override.

The complete safe workflow is in the
[structured-configurations tutorial](structured-configurations-tutorial.md).

## `canonical_morris requires charter.status scientific`

Cause: canonical Morris was configured as a demonstration or without a charter.
Remedy: do not adapt the generated discrete-OAT starter mechanically. Author a
study-specific scientific charter, parameter distributions, budget, stopping
criteria, and robustness checks as described in the [YAML reference](yaml-reference.md#canonical-morris).

## Missing or incompatible `stochastic` block

Every scientific charter requires one exact mode. Canonical Morris requires
`conditional_mean`, CRN `within_trajectory`, and an allocation; multi-scramble
Sobol requires `finite_seed_factor` with no allocation. A pilot has null selected
replications/provenance and at least its declared minimum seeds. Production has
non-empty pilot provenance and exactly its selected seed count. Do not repair a
pilot by adding seeds or changing phase in place: choose a new campaign root.

## `capped` or `infeasible` replication allocation

`capped` means the MCSE formula needs more than `maximum_replications`; it is not
evidence that the capped value meets target. `infeasible` means even the selected
structural `T*(D+1)*R` positions exceed the non-reserve budget. Revise the
declared precision, cap, design, or budget in a new campaign; never silently
lower the requirement or rely on deduplicated runs.

## Canonical Morris objective, dependence, or budget error

Canonical Morris accepts only objective `screening`, independent parameter
dependence, and a complete scientific contract. Its structural design positions,
without run-ID deduplication, must fit within
`floor(maximum_runs * (1 - reserve_fraction))`; the reserve is not available to
the initial plan. It also enforces at most 500 candidates and a deterministic
distance-computation budget. Reduce the design or increase a scientifically
justified declared budget; do not relabel FP or FF as screening.

## Canonical Morris checkpoint or bootstrap error

Use at least 100 bootstrap resamples. Checkpoints must be sorted, unique integers
of at least 2 and must end at `trajectories`; `screening_top_k` cannot exceed the
number of factors. These diagnostics do not themselves certify charter stopping
or robustness.

## `unsupported experiment.method`

The supported methods are `grid`, `discrete_oat`, `canonical_morris`, `sobol`,
and `multiscramble_sobol`. Use `discrete_oat` for an adjacent-level
demonstration, or author a study-specific `canonical_morris`
scientific-screening campaign.

## `duplicate canonical configuration and seed pair`

Cause: several Grid positions resolve to the same configuration and seed pair,
often because levels are duplicated. Remedy: deduplicate the domains or make the
configurations genuinely distinct.

## `campaign completeness ... is below threshold`

Cause: markers are missing or failure logs are present. Remedy: inspect
`logs/<runId>.log`, fix the pipeline, and rerun `run`. Do not create a marker
manually.

## `invalid completion marker`

Cause: the file was created manually, is truncated, has the wrong schema or
context, uses incorrect compression, or contains inconsistent content. Remedy:
preserve the state for diagnosis, then rebuild the run through the normal
workflow in a clean root. Manual editing makes the completeness evidence
unreliable.

## `another invocation is already running this campaign`

Cause: a `run` command already holds the campaign lock. Remedy: wait for it to
finish. After a crash, the kernel lock is released automatically; do not delete
lock files while an execution is in progress.

## `static check time budget exhausted`

Cause: the eqasim code fingerprint, plan/manifest reads, the synpp probe, or disk
measurement took
longer than the 55-second budget. Remedy: check network storage, control-file
sizes, and environment startup time.

## `synpp environment probe failed`

Cause: the invocation ends with `-m synpp` but its interpreter cannot start or
cannot `import synpp` (the probe runs the invocation with `-m synpp` replaced by
`-c "import synpp"` and requires exit code 0; no synpp output is read).
Remedy: run that command in `model.root`, then check the environment and
versions. The static probe does not run a complete simulation. When the
invocation does not end with `-m synpp`, the check has `status: skipped` and a
`note`; it does not make the result invalid.

## `check --smoke` failure

Cause: fewer than two seeds, no `sampling_rate`, a failure in the real pipeline,
a declared output file that was not written. Remedy: satisfy the smoke contract and
read the returned details. Caution: this option runs two temporary simulations at
`sampling_rate: 0.001`; it consumes compute resources and accesses real data. The
smoke check starts from the sealed model YAML and does not apply any
`execution.machine_options`. If the normal campaign depends on
`execution.machine_options.data_path`, the smoke check still uses the model's
`data_path`; make that path accessible or deliberately test from an environment
where it is valid.

## Insufficient disk or scratch space

Cause: `missingDiskBytes` or `missingScratchBytes` is positive. Remedy: increase
the quota, move `campaign_root` or `execution.scratch`, or reduce
`execution.workers`. Estimated scratch space is the maximum observed per worker
multiplied by the number of workers.

## `meta-config changed while preparing execution`

Cause: the YAML changed during `run`, or between submission and the SLURM job.
Remedy: stop concurrent editors/generators and rerun with a stable file. After a
design or context change, choose a new root.

## Constant or numerically unrepresentable Sobol variance

For demonstration `sobol`, all indicator values may be constant or bootstrap
resamples may be degenerate. Check the indicator, domains, `N`, and effective
run diversity. Do not conceal the statistical error.

For `multiscramble_sobol`, nonpositive output variance or an ineffective A/B
projected contrast at any scramble and checkpoint is fatal. Increase the first
declared base sample size or revise the scientifically justified support; no
scramble is dropped and no row bootstrap fallback is used.

The analyzer can also report `scaling erased base variance`, `output variance
exceeds float64 range`, or `output variance is below float64 range`. These errors
mean that finite values cannot support both the Sobol calculation and a variance
in the original squared units at `float64` precision. Inspect the indicator's
units and dynamic range, then rerun only after a scientifically justified
reformulation or unit change. Never clip extreme values or ignore a failed
scramble to obtain a report; publication is transactional and no partial Sobol
tables are valid.

The finite seed support is always a non-actionable nuisance. A null seed rank or
FF tolerance is intentional; its reported Sobol indices are diagnostics, not an
instruction to control seeds and not an estimate of unrestricted intrinsic noise.

## `path_length` check fails on Windows

Cause: `check` estimates the longest path eqasim will create as the length of
`execution.scratch`, plus the worker prefix for this campaign
(`esa/<campaign>/w<slot>/a<12 hex>/`, with the real campaign name and a 4-digit
slot, or the `check --smoke` layout `.smoke-<8 hex>/s/esa/smoke/w<slot>/a<12 hex>/`
when that one is longer), plus a budget of 240 characters for eqasim's own relative paths. That
budget was measured on a real Nantes run: eqasim's cache creates relative paths of
up to 231 characters (the extracted IGN IRIS archive in `data.spatial.iris`).

With that budget, the worker prefix and eqasim's cache already exceed 260
characters on their own, so **no `execution.scratch` can fit** on Windows without
long paths, even a drive root such as `C:\`. The error then reads `estimated path
length N exceeds the Windows limit of 260 characters; no execution.scratch can
fit...`. `check` fails early, before any long run, rather than in the middle of
the pipeline.

Remedy: this is the one case that needs an administrator. Either an administrator
enables Windows long paths (`LongPathsEnabled`) once, outside eqasim-sa, or an
upstream eqasim change (extracting the IRIS archive into a shorter directory)
removes the need. `check` reads that registry value, read-only, and passes when it
is enabled; eqasim-sa never changes it. If a future eqasim needs a smaller budget,
`check` instead names the longest `execution.scratch` that fits (`choose an
execution.scratch of at most N characters`); put the scratch on a short path such
as `C:\esa` in that case. On Linux and macOS the limits are 4096 and 1024
characters and `check` only warns.

## Campaign or output name refused

Campaign names are limited to 64 characters. On every operating system, eqasim-sa
refuses the Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`,
`LPT1`-`LPT9`, with or without an extension, any case), names ending in a dot or a
space, and names that differ only by case. This keeps a campaign created on Linux
transferable to Windows and macOS. Rename the campaign, output, or indicator.

## Line endings and `.gitattributes`

Copying or checking out files on Windows may convert LF to CRLF. eqasim-sa hashes
text inputs after normalizing CRLF to LF, so a CRLF copy produces the same
identities as on the cluster: the eqasim code fingerprint
(`eqasimSourceSha256`), the `uv.lock` hash (`lockfileSha256`), the canonical JSON
form of the data manifest (`dataManifestSha256`), the `eqasim-sa` software
identity, the built-in indicator definitions, and the eqasim configuration in
the execution evidence (`model_config_sha256`). Data files are still hashed byte
for byte. `init` also writes a `.gitattributes` (never overwriting yours) forcing
LF for YAML, JSON, and `*.lock` files. If `check` reports a mismatch after a
transfer, compare the files on both machines before suspecting the data.

## SLURM backend on Windows or macOS

The `slurm` backend requires Linux. `check` reports the `backend` item as failed
with `the slurm backend requires Linux; run eqasim-sa on the cluster`, and `run`
refuses with the same message. Prepare the study on your PC, copy it to the
cluster with `scp`, `sftp`, or any copy tool, render the machine paths again (the
`__MACHINE_*__` markers of `protocols/*/` with `render_protocol.py`, or edit the
YAML paths), and run `check`, `plan`, and `run` on the cluster. The PC never
submits jobs.

## `campaign runs were executed on another machine`

Full message: `campaign runs were executed on another machine or with other
inputs: <causes> (differing: ...); prepare a new campaign on this machine`.
Cause: completed runs of the campaign carry execution evidence (executable path and
hash, child environment, invocation, model config, `eqasim-sa` identity) that
differs from this machine. A campaign runs entirely on one machine, so `run`
refuses to resume it and `check` reports the `same_machine` item as failed. The
message names each cause in plain words, followed by the `differing` fields:

| Cause | Fields | Typical reason |
| --- | --- | --- |
| `executable differs` | `executable_sha256`, `invocation` | another machine, or the eqasim environment was reinstalled or moved |
| `environment differs` | `environment_sha256`, `environment_variables` | another machine, or a different shell environment (virtual environment, `PATH`, other variables) |
| `model configuration changed` | `model_config_sha256` | the eqasim `model.config` file was edited since the first runs |
| `eqasim-sa installation changed` | `eqasim_sa_sha256` | `eqasim-sa` was updated or reinstalled |

Remedy: prepare a new campaign (new `campaign_root`) on this machine, or restore
what changed. Per-session variables (terminal and SSH session identifiers such as
`WT_SESSION`, `TERM_SESSION_ID`, `SSH_CONNECTION`, `DISPLAY`, `SHLVL`, `PWD`) are
ignored, so opening a new terminal does not trigger the refusal; other variables
are part of the environment hash, so resume from an environment like the one that
started the campaign. For the `slurm` backend, `same_machine` is skipped on the submitting
host and enforced inside the Slurm job.

## Analyzing on another machine

`analyze` does not need the data tree or the scratch, and never compares the
execution evidence with the analyzing machine. It needs: the campaign folder copied
back (`scp` or `sftp`), the eqasim folder with the same code, the same
`uv.lock` and data manifest, and the machine paths rendered again for that machine.
`metaConfigSha256` is published provenance, not an equality constraint, so
different machine paths do not block analysis.

## Ctrl+C on Windows

The eqasim child process is started in its own process group inside a Windows Job
Object. When you press Ctrl+C, eqasim-sa's interrupt handling terminates the whole
process tree, so no descendant keeps files open in the scratch. The run is recorded
as interrupted and can be resumed with `run`.

## Unsupported filesystem

Keep `campaign_root`, `execution.scratch`, and the data on a local NTFS, APFS,
ext4, or xfs disk, or on the cluster's shared filesystem. FAT/exFAT volumes and SMB
shares are not supported for campaigns (locks and atomic publication are not
guaranteed). They are not detected either: eqasim-sa does not refuse them, so
verify where these paths live.

## Safety warnings

- Never modify `plan.parquet`.
- Never create, copy, or modify `runs/<runId>.parquet`.
- Do not delete locks, scratch space, or a campaign while a process or job is active.
- Do not reuse a root after changing the model, data, lockfile, or version.
- Never treat an archive alone as a complete run.

For definitions of marker, archive, scratch, and machine option, see the
[glossary](glossary.md).
