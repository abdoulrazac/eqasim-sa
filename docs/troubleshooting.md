# Troubleshooting

[Français](../docs-fr/troubleshooting.md)

## `model Git repository must be clean`

Cause: a tracked file has been modified, an untracked file is present, the index
differs, or a change occurred during loading. Remedy: inspect
`git status --short` at the model root, then deliberately commit or remove the
changes. Do not bypass this check: the commit is included in `contextHash`.

## `model.config does not match its sealed Git blob`

Cause: the file being read is not identical to the version in `HEAD`. Remedy:
restore or commit the configuration, then use a new campaign if a plan had
already been created.

## `plan differs; choose a new campaign_root`

Cause: the design, parameters, context, package version, or artifact requirements
differ under a root that has already been sealed. Remedy: choose a new
`campaign_root`. Do not replace `plan.parquet`.

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
`execution.overrides`.

## Type error in `parameters`

Common causes: a boolean, `null`, mixed levels, a non-finite float, or an
incorrectly ordered historical-Morris/discrete-OAT domain. Remedy: keep the type
homogeneous and numbers finite. Where a float is required, write `1.0`, not `1`.

## Named structured factor errors

- `factor 'matching_attributes' must contain exactly ...`: a demonstration
  declaration has exactly `configurations`; a scientific declaration has exactly
  `configurations` and `distribution`. Do not combine `values` with
  `configurations`.
- `unknown factor` or `model config value must be a list of strings`: the sealed
  model YAML must already contain active `config.matching_attributes` as a list
  of strings. Commit that YAML and keep the model repository clean before
  planning; eqasim-sa does not create or coerce the key.
- `must be a non-empty ordered list...`, `distinct payloads`, or `payload
  aliases`: every configuration payload must be a non-empty list of unique,
  non-empty strings, and no two labels may resolve to the same ordered list.
  Labels must also be non-empty strings without NUL.
- `does not support structured factors`: `morris`, `discrete_oat`, and
  `canonical_morris` all refuse named structured payloads. Use Grid for a
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

## `morris` is rejected by a scientific charter

Historical `method: morris` is readable only as legacy demonstration provenance.
Use `discrete_oat` for a new adjacent-level demonstration, or author a
study-specific `canonical_morris` scientific-screening campaign.

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

Cause: Git access, plan/manifest reads, the synpp probe, or disk measurement took
longer than the 55-second budget. Remedy: check network storage, control-file
sizes, and environment startup time.

## `synpp environment probe failed`

Cause: the invocation does not start synpp as expected or does not respond with
`Config file does not exist:` for the probe file. Remedy: execute the
`execution.invocation` list in `model.root`, then check the environment and
versions. The static probe does not run a complete simulation.

## `check --smoke` failure

Cause: fewer than two seeds, no `sampling_rate`, a failure in the real pipeline,
a missing artifact, or invalid Parquet. Remedy: satisfy the smoke contract and
read the returned details. Caution: this option runs two temporary simulations at
`sampling_rate: 0.001`; it consumes compute resources and accesses real data. The
smoke check starts from the sealed model YAML and does not apply any
`execution.overrides`. If the normal campaign depends on
`execution.overrides.data_path`, the smoke check still uses the model's
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

For historical `sobol`, all indicator values may be constant or bootstrap
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
`execution.scratch`, plus the fixed worker prefix (`esa/<campaign>/w<slot>/c<12 hex>/`),
plus a budget of 240 characters for eqasim's own relative paths. That budget was
measured on a real Nantes run: eqasim's cache creates relative paths of up to 231
characters (the extracted IGN IRIS archive in `data.spatial.iris`). The error reads
`estimated path length N exceeds the Windows limit of 260 characters; choose a
shorter execution.scratch (for example C:\esa)...`.

Remedy: put `execution.scratch` on a very short path such as `C:\esa`. Be aware
that, for Nantes, even a minimal scratch cannot fit in 260 characters, because
most of the length is eqasim's own cache; `check` therefore fails early, before any
long run, rather than in the middle of the pipeline. If Windows long paths are
enabled (`LongPathsEnabled`), `check` reads that registry value, read-only, and
passes; eqasim-sa never changes it. Enabling it requires an administrator, once,
outside eqasim-sa. An upstream eqasim change (extracting the IRIS archive into a
shorter directory) would remove the need. On Linux and macOS the limits are 4096
and 1024 characters and `check` only warns.

## Campaign or artifact name refused

Campaign names are limited to 64 characters. On every operating system, eqasim-sa
refuses the Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`,
`LPT1`-`LPT9`, with or without an extension, any case), names ending in a dot or a
space, and names that differ only by case. This keeps a campaign created on Linux
transferable to Windows and macOS. Rename the campaign, artifact, or indicator.

## Line endings and `.gitattributes`

A Git checkout on Windows may convert LF to CRLF. eqasim-sa hashes text inputs
after normalizing CRLF to LF, so a CRLF checkout produces the same identities as
on the cluster: the `uv.lock` hash (`lockfileSha256`), the canonical JSON form of
the data manifest (`dataManifestSha256`), the `eqasim-sa` software identity,
built-in indicator and plugin text (binary files stay raw), and the `model.config`
Git check, which uses `git hash-object --path` so that Git's own conversion rules
apply. Data files are still hashed byte for byte. `init` also writes a
`.gitattributes` (never overwriting yours) forcing LF for YAML, JSON, and `*.lock`
files. If `check` reports a mismatch after a transfer, compare the files with
`git status` and `git diff` before suspecting the data.

## SLURM backend on Windows or macOS

The `slurm` backend requires Linux. `check` reports the `backend` item as failed
with `the slurm backend requires Linux; run eqasim-sa on the cluster`, and `run`
refuses with the same message. Prepare the study on your PC, copy it to the
cluster with Git, `scp`, or `sftp`, render the machine paths again (the
`__MACHINE_*__` markers of `protocols/*/` with `render_protocol.py`, or edit the
YAML paths), and run `check`, `plan`, and `run` on the cluster. The PC never
submits jobs.

## `campaign runs were executed on another machine`

Full message: `campaign runs were executed on another machine (differing: ...);
prepare a new campaign on this machine`. Cause: completed runs of the campaign
carry execution evidence (executable path and hash, child environment, invocation,
model config, `eqasim-sa` identity) that differs from this machine. A campaign runs
entirely on one machine, so `run` refuses to resume it and `check` reports the
`same_machine` item as failed. The `differing` list names the fields. Remedy:
prepare a new campaign (new `campaign_root`) on this machine. Note that the
environment hash covers the session environment: a very different shell
environment on the same machine, for example a different virtual environment or
`PATH`, can also trigger the refusal; resume from the environment that started the
campaign. For the `slurm` backend, `same_machine` is skipped on the submitting
host and enforced inside the Slurm job.

## Analyzing on another machine

`analyze` does not need the data tree or the scratch, and never compares the
execution evidence with the analyzing machine. It needs: the campaign folder copied
back (`scp` or `sftp`), the eqasim repository at the same commit, the same
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
guaranteed).

## Safety warnings

- Never modify `plan.parquet`.
- Never create, copy, or modify `runs/<runId>.parquet`.
- Do not delete locks, scratch space, or a campaign while a process or job is active.
- Do not reuse a root after changing the model, data, lockfile, or version.
- Never treat an archive alone as a complete run.

For definitions of marker, archive, scratch, and override, see the
[glossary](glossary.md).
