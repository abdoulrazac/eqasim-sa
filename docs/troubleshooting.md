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
incorrectly ordered Morris domain. Remedy: keep the type homogeneous and numbers
finite. Where a float is required, write `1.0`, not `1`.

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

## Constant-variance Sobol analysis

Cause: all indicator values are constant, or the bootstrap resamples are
degenerate. Remedy: check the indicator, domains, `N`, and the effective
diversity of the runs. Do not conceal the statistical error.

## Safety warnings

- Never modify `plan.parquet`.
- Never create, copy, or modify `runs/<runId>.parquet`.
- Do not delete locks, scratch space, or a campaign while a process or job is active.
- Do not reuse a root after changing the model, data, lockfile, or version.
- Never treat an archive alone as a complete run.

For definitions of marker, archive, scratch, and override, see the
[glossary](glossary.md).
