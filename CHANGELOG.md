# Changelog

## Unreleased

- New `eqasim-sa plot --meta-config PATH [--figure NAME ...]` command. It draws,
  from the published analysis generation and after verifying the sha256 of each
  published output it reads, only the figures valid for the campaign's method and published tables
  (`output-summary`, `grid-response`, `oat-effects`, `sobol-indices`,
  `morris-plane`, `morris-convergence`, `morris-rank-reversals`,
  `morris-noise-floor`, `multiscramble-indices`, `multiscramble-convergence`,
  `multiscramble-by-scramble`), as PNG files plus `figures.json` in
  `figures/<generationId>/`. The figures are not part of the sealed generation
  and nothing is written under `analysis/`. Rerunning `eqasim-sa init` on the
  project accepts the `figures/` directory that `plot` writes. See
  `docs/workflow-and-results.md`.
- Fixes from the end-to-end test on real eqasim and Nantes data:
  - BREAKING: `check` items set `error` only for `failed` and `warning`;
    `passed` and `skipped` items explain themselves in `note`
    (`path_length` gives its estimate there, `same_machine` its skip reason).
  - `check --smoke`: `elapsedSeconds` is the duration of the whole command,
    smoke runs included.
  - `estimate`: Grid now fills `formula` (`R*prod(L)`) and `formulaInputs`
    (`{"R": seeds, "L": {parameter: number of values}}`) like the other
    methods; both are `null` only in a failed result.
  - `show-config` writes `execution.python` when it derives from
    `execution.invocation` (the invocation without its final `-m synpp`).
  - `analyze`: new `analyses` field, `{name: {"computed": n, "cached": m}}`
    per analysis read by an indicator; the failure envelope also carries
    `analyses` and `warnings`.
  - `init`: on Windows, a model, data, or scratch folder on another drive is
    written as an absolute path in the starters (no relative path exists);
    the generated README states the starters' `workers: 2`; an `init` rerun
    accepts the `collected/` folder that a failed run leaves in its scratch
    attempt.
  - Docs: real scratch layout (`scratch/esa/<campaign>/w<slot>/`), the nine
    `check` items, what can be deleted and when, the analysis example rewritten
    on `trips` + `activities_spatial` with the default `tv_distance`
    indicators kept (the mode-share analysis, which needs
    `mode_choice: true`, is now a separate conditional snippet), paths
    anywhere (not nested), and resuming `run` from another shell or IDE
    (`same_machine`).
- Fixes from the second end-to-end test on real eqasim and Nantes data:
  - `init` rerun (identical or `--regenerate-data-manifest`) works again on a
    project that has been used: it accepts the starters' campaign folders
    (`<project>-calibration`, `-grid`, `-discrete_oat`, `-sobol`, derived from
    the rendered starters) instead of refusing them as foreign content, and
    accepts `configs/analyses/` (files and subfolders, no symbolic links,
    content not read). Other entries in `configs/` and `campaigns/` are still
    refused; `campaigns/<method>` folders, which no starter creates, are now
    refused too.
  - `init` rerun: an edited starter is no longer refused. Only the values
    `init` fills in are compared (`model.root`, `model.config`,
    `model.data_manifest`, `execution.scratch`, `execution.invocation`,
    `execution.machine_options.data_path`, `campaign`, `campaign_root`); a
    difference is refused with `init inputs changed: <key> in configs/<file>`,
    an unreadable starter with `init cannot parse configs/<file>: ...`.
    Otherwise the starter, like an edited README, is kept as it is and listed
    in the new `kept` result field. A missing starter or README is recreated
    and listed in `restored`, only once every check has passed. Starters are
    checked before campaign folders, and the archive files accepted in the
    campaigns follow the outputs of the starters as they are on disk.
  - `init`: a failed result reports the real `elapsedSeconds`; the generated
    README names the actual `--model-config` instead of always `config.yml`.
  - `VIRTUAL_ENV` (eqasim-sa's own environment) is no longer passed to
    eqasim, the synpp probe, analysis scripts, or the analysis interpreter
    probe, and no longer enters the `same_machine` environment identity, so uv
    no longer warns that it does not match eqasim's `.venv`.
  - New `eqasim-sa --version` (prints `unknown` when the package metadata is
    missing).
  - Docs: exact rules of an `init` rerun; "Trying a script by hand" with
    placeholders for the eqasim path and the campaign, and uv's fallback to
    another Python when the path does not exist; the example analysis numbers
    presented as indicative; `requiredDiskBytes` versus `estimatedDiskBytes`
    in `check`; what `calibrationRuns` of `estimate` counts.
- Fix: commands could hang forever at exit after printing their result;
  Arrow reads (Parquet and CSV) no longer use worker threads.
- BREAKING: analysis scripts. A new optional `analyses` block declares Python
  scripts (`script`, relative to the meta-configuration or absolute; `inputs`,
  archived `.csv` or `.gpkg` outputs; optional `category`, `simulated`,
  `references`) whose `compute(files, parameters)` returns a
  `pandas.DataFrame`; the table is an analysis output that `tv_distance`,
  `share`, and `value` read like an output. `analyze` runs the scripts read by
  indicators, after the runs and without eqasim, synpp, cache, or data, with
  eqasim's interpreter (new `execution.python`, by default
  `execution.invocation` without `-m synpp`, or `uv run --locked python`), on
  copies of the archived files, `execution.workers` at a time. Results are
  cached in `analyses-cache/` under a fingerprint (domain
  `eqasim-sa:analysis-result:v1`) of the script, inputs, run parameters, and
  Python environment (Python, pandas, numpy, pyarrow versions and eqasim's
  `uv.lock`); an altered cache entry is recomputed and reported in the new
  `warnings` field of the `analyze` result. A script error fails the whole
  analysis and its log is kept in `analyses-cache/logs/`. A
  `primary_indicator` cannot read an analysis output, and default indicators
  ignore them. `check` does not run scripts: its smoke runs skip the
  indicators reading analysis outputs. Indicator definitions use domain
  `eqasim-sa:indicator-definition:v4` (an analysis output is described by
  `{name, analysis, inputs, separator}`); the analysis manifest is version 8
  with `provenance.analyses`, and `captured-run-set.json` is schema version 2
  with per-run `analyses` records. See `docs/analysis-scripts.md`.
- BREAKING: the plan seals the eqasim configuration as `modelDocument` (its
  canonical form without the machine-only keys and `random_seed`); a plan
  without it is refused. `analyze` checks the meta-configuration against the
  sealed plan, so editing the eqasim code, configuration, or `uv.lock` after
  the runs does not block it, while `run` refuses to resume with a message
  naming what changed (`eqasim config changed since plan`). Editing a
  machine-only key such as `data_path` or `processes` changes neither the plan
  nor the resumption.

- BREAKING: eqasim-sa runs the official, unmodified eqasim and no longer needs
  Git. `model.root` is any eqasim folder (clone, copy, or archive); eqasim-sa
  never calls `git`. The eqasim code is identified by a content fingerprint
  (`*.py`, `*.java`, `pom.xml`, `pyproject.toml`, `uv.lock` under `model.root`,
  LF-normalized; hidden folders, `__pycache__`, virtual and conda environments,
  the eqasim `working_directory` and `output_path`, and links are ignored),
  checked again before every run. It replaces the eqasim commit: the context
  descriptor key is `eqasimSourceSha256` (domain `eqasim-sa:context:v3`), the
  execution evidence key is `modelSourceSha256` (schema 3, domain
  `eqasim-sa:execution-evidence:v3`), and indicator definitions use domain
  `eqasim-sa:indicator-definition:v3`. `model.config` is required and read
  relative to `model.root` (or absolute); `init --model-config` defaults to
  `config.yml`. synpp runs the `run:` list of the eqasim configuration unchanged;
  a known output left at its default file whose stage is missing from `run:` is
  refused at load time (add `analysis.synthesis.population` to the official
  `config.yml` for `age`, `distance`, `nbr_vehicle`, ...). `outputs` (at least
  one) accept `file`, `separator`, `category`, `simulated`, and `references`,
  with defaults for the twelve known eqasim outputs; each declared file is
  archived byte for byte as `<name><suffix>` with its `sourcePath` in the run
  manifest, and only name, `file`, and `separator` are sealed in the plan
  (`outputsDeclaration`). CSV outputs are read as text (a first column with an
  empty header is dropped) and numbers are parsed explicitly. Indicators are
  `tv_distance`, `share` (`share(output, category)`), and `value`
  (`value(output, column[, column=value])`); `share.category` and `where` values
  must be quoted YAML strings. Custom indicator plugins are removed: compute
  custom quantities in an analysis script (see above) or in an eqasim analysis
  stage read with `value`. The
  `init` starters vary only `sampling_rate` and keep the population files. The
  synpp probe of `check` runs the invocation with `-m synpp` replaced by
  `-c "import synpp"`. Plans, markers, and archives of earlier versions
  (`eqasimCommit`, `modelCommit`, Parquet output archives) are not read.

- BREAKING: simplified meta-configuration format; files in the previous format
  are rejected, and an unknown key error lists the keys allowed at its level.
  Root keys are `campaign`, `campaign_root`, `charter`, `model`, `experiment`,
  `parameters`, `analysis`, `outputs`, `indicators`, `execution`, and
  `calibration`. `model` holds `root`, `config`, `data_manifest`, and an
  optional `lockfile`. Simulation seeds are always written in
  `experiment.seeds` (replications for Grid, discrete OAT, and canonical Morris;
  the support of the `seed` factor for Sobol and multi-scramble Sobol, which
  also requires `experiment.seeds_provenance`); a `seed` parameter is refused.
  A parameter may be a plain list (`sampling_rate: [0.001, 0.002]`); a
  scientific charter still requires `values` with `distribution`. `outputs`
  declare `category`, `simulated`, and `references` columns. Each `indicators`
  entry has one key, such as `tv_distance` (with `output` and `reference`,
  `output`, or `outputs`); without `indicators`,
  `tv_distance` is computed for every output against each of its references.
  Infrastructure options live in `execution.machine_options`. Defaults:
  `campaign_root` `../campaigns/<campaign>`, `model.lockfile`
  `<model.root>/uv.lock`, `design_seed` derived from the campaign name,
  `completeness_threshold` `1.0` (`1` is accepted), `bootstrap_resamples` 999,
  `confidence_level` 0.95, and, when `execution` is present, `backend` local,
  `workers` 1, and the `uv run --locked python -m synpp` invocation. The `init`
  starters use these defaults and are much shorter. The new
  `eqasim-sa show-config META_CONFIG` validates a meta-configuration and prints
  it as YAML with every default applied (paths as written); an
  invalid file prints the error on standard error and exits with code 1, with
  no JSON envelope.
- BREAKING: drops all backward compatibility (dev phase, no existing campaigns).
  Removed: reading legacy scientific plans (`canonical_morris` and
  `multiscramble_sobol` design v1/v2; only design v3 is accepted);
  completion markers without a version or at v1 (`legacy_unbound`; only v2
  markers bound to an archive manifest); execution evidence schema v1; the
  SLURM active-job journal v1 and the `<campaign>/.submitit` directory; the
  `.eqasim-sa-analysis` analysis state directory (only `analysis/` remains);
  flat analysis outputs at the campaign root and their cleanup; analysis
  generation manifest v6 (only immutable generations are read); `init` no longer
  tolerates `.submitit`, `.eqasim-sa-tmp`, or `.eqasim-sa-run-locks` in a
  campaign; and the `charter.legacy` flag (a missing charter still means a
  demonstration charter).
- BREAKING: removes `method: morris` ("historical Morris") and its
  `morris-ranking.parquet` output; a configuration using it is rejected. Use
  `discrete_oat` (adjacent-level demonstration) or `canonical_morris`
  (scientific screening).
- BREAKING: plain `method: sobol` stays as a noninferential demonstration (the
  generated `03-sobol.yml`); its inference status is renamed
  `legacy_noninferential` -> `demonstration_noninferential`, and "historical
  Sobol" is now "demonstration Sobol" in the documentation.

- Supports Windows 10+, macOS 13+, and Linux natively (Python 3.13 via uv, no
  administrator rights) with the `local` backend; verification on Windows and
  macOS is pending (see `docs/platform-verification.md`); the `slurm` backend remains
  Linux only and is refused early by `check` and `run` elsewhere. Exception: on
  Windows without long paths no scratch fits eqasim's own cache, so a real
  eqasim run needs an administrator to enable `LongPathsEnabled` once (or an
  upstream eqasim change); `check` reports it before any run.
- Adds a platform layer (locks, atomic replace, fsync, links, process-tree
  supervision with a Windows Job Object) and path-based durable I/O; all text I/O
  is explicit UTF-8.
- Shortens the worker layout to `<scratch>/esa/<campaign>/w<slot>/c<12 hex>/`;
  refuses Windows reserved names, trailing dots/spaces, case-only collisions, and
  campaign names over 64 characters on every OS.
- Adds `check` items `backend`, `path_length` (uses the real campaign name, reads
  `LongPathsEnabled` read-only on Windows, and suggests a shorter scratch only
  when one would fit), and `same_machine`; `run` refuses to resume a campaign whose
  completed runs carry different execution evidence.
- Makes identities line-ending independent (CRLF normalized to LF for the
  lockfile, data manifest canonical JSON, software and indicator identities);
  `init` writes a `.gitattributes`.
- Bumps the software identity hash domain to `v2` (no backward compatibility;
  see above for the context and indicator definition domains).
- Adds a multi-OS CI workflow and `docs/platform-verification.md`.
- Declares the project under GPL-2.0-only and adds bilingual contribution
  guidance and package README metadata.
- Adds generic named structured-list factors for Grid, demonstration Sobol, and
  multi-scramble Sobol. Plans store readable labels while resolved ordered lists
  determine `runId` and execution snapshots; aliases and malformed payloads are
  rejected.
- Adds plan schema v3 only when a plan seals canonical
  `structuredFactorsVersion: 1` and `structuredFactors` metadata. Unstructured
  plans retain their existing v1/v2 identities and byte behavior.
- Refuses structured factors for discrete OAT and canonical
  Morris because their nominal configuration order has no defensible adjacent or
  probability-space meaning.

## 0.3.0 - 2026-08-21

- Keeps `init` demonstration-only. Its calibration, Grid, discrete OAT, and
  single-scramble Sobol starters validate plumbing but support no inferential or
  decision claim.
- Keeps historical `method: morris` and `method: sobol` plans readable as
  demonstration or legacy noninferential provenance. This release performs no
  semantic migration and adds no compatibility shim.
- Supports scientific canonical Morris only for screening, using plan schema v2
  and canonical Morris design metadata v1.
- Supports scientific multi-scramble Sobol only for factor prioritization (FP)
  or factor fixing (FF), using plan schema v2 and multi-scramble Sobol design
  metadata v1 with independent scrambles.
- Publishes analysis manifest v6, explicit inference statuses, convergence
  evidence, and governed reports. Scientific evidence remains conditional on
  declared stopping and robustness work; this release never auto-authorizes a
  claim.
- Documents and tests exact method, objective, stochastic, charter, schema, and
  package boundaries in English and French.
- Keeps `protocols/nantes-scientific/` as repository-only protocol material.
  Fake/test-harness rendering and refusal checks are not evidence that any real
  Nantes campaign was planned, simulated, analyzed, or executed.

There is no semantic migration from historical plans to scientific plans and no
claim that the Nantes protocol has been executed.
