# Changelog

## Unreleased

### Added

- The `eqasim-sa` command first checks that it runs on Python 3.13. Started
  from another Python (for example a uv tool environment whose interpreter
  link now points to another version), it prints `eqasim-sa requires Python
  3.13, but this environment runs Python X.Y.Z (...)` with the reinstall
  command and exits with code 1, instead of failing inside a compiled
  dependency (`No module named 'pyarrow.lib'`).
- Each GitHub release carries `constraints.txt`, the runtime dependency
  versions of `uv.lock`; `uv tool install --constraints <url> ...` installs
  exactly the tested versions.

### Changed

- The `test` extra declares `cloudpickle` and `pillow`, which the tests import
  directly, at the versions already locked.

## 0.4.0 - 2026-11-23

Entries marked BREAKING change a file format, an identity, a JSON field, or a
message that scripts may rely on. Plans, markers, archives, and analyses
written by earlier versions are not read: plan and run the campaigns again.

### Added

- `eqasim-sa plot --meta-config PATH [--figure NAME ...]` draws, from the
  published analysis generation and after checking the sha256 of every
  published output it reads, only the figures valid for the campaign's method
  and its published tables (`output-summary`, `grid-response`, `oat-effects`,
  `sobol-indices`, `morris-plane`, `morris-convergence`,
  `morris-rank-reversals`, `morris-noise-floor`, `multiscramble-indices`,
  `multiscramble-convergence`, `multiscramble-by-scramble`). It writes PNG
  files and `figures.json` to `figures/<generationId>/` in the campaign folder;
  the figures are not part of the sealed generation and nothing is written
  under `analysis/`. Figure text is in English, the inference status is
  stamped on each figure, and long axis labels are wrapped inside the axes.
  An error is printed on standard error, without a JSON envelope.
- Analysis scripts: a new optional `analyses` block declares Python scripts
  (`script`, relative to the meta-configuration or absolute; `inputs`, archived
  `.csv` or `.gpkg` outputs; optional `category`, `simulated`, `references`)
  whose `compute(files, parameters)` returns a `pandas.DataFrame`. The table is
  an analysis output that `tv_distance`, `share`, and `value` read like an
  output. `analyze` runs the scripts read by indicators, after the runs and
  without eqasim, synpp, cache, or data, with eqasim's interpreter (new
  `execution.python`; by default `execution.invocation` without its final
  `-m synpp`, or `uv run --locked python` without an `execution` block), on
  copies of the archived files, `execution.workers` at a time. Results are
  cached in `analyses-cache/` under a fingerprint of the script, the inputs,
  the run parameters, and the Python environment (versions of Python, pandas,
  numpy, pyarrow, geopandas, shapely, and pyogrio, and the eqasim lockfile);
  an altered cache entry is recomputed and reported in the new `warnings`
  field of the `analyze` result. A script error fails the whole analysis and
  its log is kept in `analyses-cache/logs/`. Scripts can be added after the
  runs, for example on another computer after copying the campaign folder
  back. A `primary_indicator` cannot read an analysis output, and default
  indicators ignore them. `check` does not run scripts: its smoke runs skip
  the indicators that read analysis outputs. See `docs/analysis-scripts.md`.
- `analyze` result: new `analyses` field, `{name: {"computed": n, "cached":
  m}}` per analysis read by an indicator, also present with `warnings` in a
  failed result.
- `eqasim-sa show-config META_CONFIG` validates a meta-configuration and
  prints it as YAML with every default applied (paths as written, and
  `execution.python` when it derives from `execution.invocation`). An invalid
  file prints the error on standard error and exits with code 1, without a
  JSON envelope.
- `eqasim-sa --version` (prints `unknown` when the package metadata is
  missing).
- `check` items `backend`, `path_length` (uses the real campaign name, reads
  `LongPathsEnabled` read-only on Windows, and suggests a shorter scratch only
  when one would fit), and `same_machine`. `check` now has nine items.
- Platform layer for Windows 10+, macOS 13+, and Linux with Python 3.13 via uv
  and no administrator rights (locks, atomic replace, fsync, links,
  process-tree supervision with a Windows Job Object, explicit UTF-8 text
  I/O). The test suite has been run on Linux only; Windows and macOS
  verification is pending until the CI workflow has run. A CI
  workflow runs the tests on Ubuntu, macOS, and Windows. The `slurm` backend
  remains Linux only and is refused early by `check` and `run` elsewhere. On
  Windows without long paths no scratch fits eqasim's own cache, so a real
  eqasim run needs an administrator to enable `LongPathsEnabled` once; `check`
  reports it before any run.
- Named structured-list factors for Grid, demonstration Sobol, and
  multi-scramble Sobol: plans store readable labels while the resolved ordered
  lists determine `runId` and the execution snapshot; aliases and malformed
  payloads are refused. A plan with structured factors has plan schema v3 and
  seals `structuredFactorsVersion: 1` and `structuredFactors`. Discrete OAT
  and canonical Morris refuse structured factors (their nominal order has no
  adjacent or probability-space meaning).
- The project is distributed under GPL-2.0-only, with contribution guidance in
  English and French.
- Releases are published on GitHub only: pushing a `v*` tag runs
  `.github/workflows/release.yml` (lockfile check, tests, build, wheel content
  check) and creates the GitHub release with the wheel and the sdist attached.

### Changed

- BREAKING: eqasim-sa runs the official, unmodified eqasim and no longer needs
  Git. `model.root` is any eqasim folder (clone, copy, or archive); eqasim-sa
  never calls `git`. The eqasim code is identified by a content fingerprint
  (`*.py`, `*.java`, `pom.xml`, `pyproject.toml`, `uv.lock` under
  `model.root`, LF-normalized; hidden folders, `__pycache__`, virtual and conda
  environments, the eqasim `working_directory` and `output_path`, and links
  are ignored), checked again before every run. `model.config` is required and
  read relative to `model.root` (or absolute); `init --model-config` defaults
  to `config.yml`. synpp runs the `run:` list of the eqasim configuration
  unchanged; a known output left at its default file whose stage is missing
  from `run:` is refused at load time (add `analysis.synthesis.population` to
  the official `config.yml` for `age`, `distance`, `nbr_vehicle`, ...). The
  synpp probe of `check` runs the invocation with `-m synpp` replaced by
  `-c "import synpp"`.
- BREAKING: simplified meta-configuration format; files in the previous format
  are refused, and an unknown key error lists the keys allowed at its level.
  Root keys are `campaign`, `campaign_root`, `charter`, `model`, `experiment`,
  `parameters`, `analysis`, `outputs`, `analyses`, `indicators`, `execution`,
  and `calibration`. `model` holds `root`, `config`, `data_manifest`, and an
  optional `lockfile`. Simulation seeds are always written in
  `experiment.seeds` (replications for Grid, discrete OAT, and canonical
  Morris; the support of the `seed` factor for Sobol and multi-scramble Sobol,
  which also requires `experiment.seeds_provenance`); a `seed` parameter is
  refused. A parameter may be a plain list (`sampling_rate: [0.001, 0.002]`);
  a scientific charter still requires `values` with `distribution`.
  Infrastructure options live in `execution.machine_options`. Defaults:
  `campaign_root` `../campaigns/<campaign>`, `model.lockfile`
  `<model.root>/uv.lock`, `design_seed` derived from the campaign name,
  `completeness_threshold` `1.0`, `bootstrap_resamples` 999 and
  `confidence_level` 0.95 for the methods that use them, and, when `execution`
  is present, `backend` local, `workers` 1, and the `uv run --locked python -m
  synpp` invocation. The `init` starters use these defaults and are much
  shorter.
- BREAKING: `outputs` replaces `artifacts`. At least one output is required;
  each accepts `file`, `separator`, `category`, `simulated`, and `references`,
  with defaults for the twelve known eqasim outputs. Each declared file is
  archived byte for byte as `<name><suffix>` with its `sourcePath` in the run
  manifest; only the name, `file`, and `separator` are sealed in the plan
  (`outputsDeclaration`). CSV outputs are read as text (a first column with an
  empty header is dropped) and numbers are parsed explicitly.
- BREAKING: indicators are `tv_distance`, `share` (`share(output, category)`),
  and `value` (`value(output, column[, column=value])`); each `indicators`
  entry has one key. `share.category` and `where` values must be quoted YAML
  strings. Without an `indicators` block, `tv_distance` is computed for every
  output that has references, against each of them. Custom indicator plugins
  are removed: compute custom quantities in an analysis script or in an eqasim
  analysis stage read with `value`.
- BREAKING: the plan seals the eqasim configuration as `modelDocument` (its
  canonical form without the machine-only keys and `random_seed`). `analyze`
  checks the meta-configuration against the sealed plan, so editing the eqasim
  code, configuration, or `uv.lock` after the runs does not block it, while
  `run` refuses to resume with a message naming what changed (`eqasim config
  changed since plan`). Editing a machine-only key such as `data_path` or
  `processes` changes neither the plan nor the resumption.
- BREAKING: identity and file versions. Context descriptor key
  `eqasimSourceSha256` (domain `eqasim-sa:context:v3`) replaces the eqasim
  commit; execution evidence uses `modelSourceSha256` (schema 3, domain
  `eqasim-sa:execution-evidence:v3`); indicator definitions use domain
  `eqasim-sa:indicator-definition:v4` (an analysis output is described by
  `{name, analysis, inputs, separator}`); the software identity domain is
  `v2`; the analysis manifest is version 8 with `provenance.analyses` and
  `provenance.analysisEnvironments`; `captured-run-set.json` is schema
  version 2 with per-run `analyses` records; analysis results use the
  fingerprint domain `eqasim-sa:analysis-result:v1`. Identities no longer
  depend on line endings (CRLF normalized to LF for the lockfile, the data
  manifest, and the software and indicator identities), and `init` writes a
  `.gitattributes`.
- BREAKING: every command prints its JSON response on one line with sorted
  keys.
- BREAKING: `check` items set `error` only for `failed` and `warning`;
  `passed` and `skipped` items explain themselves in `note` (`path_length`
  gives its estimate there, `same_machine` its skip reason). With `--smoke`,
  `elapsedSeconds` is the duration of the whole command, smoke runs included.
- BREAKING: a failed `estimate` reports `null`, not `0`, for
  `designPositions`, `runs`, `calibrationRuns`, `estimatedDiskBytes`, and
  `estimatedScratchBytes`. Grid now fills `formula` (`R*prod(L)`) and
  `formulaInputs` (`{"R": seeds, "L": {parameter: number of values}}`) like
  the other methods; both are `null` only in a failed result.
- BREAKING: `estimate` and the `diskSpace` item of `check` without any
  completed calibration run say `no completed calibration run yet: run
  campaign <campaign> first` when no calibration is referenced, or `... run
  the calibration campaign <campaign> first` (`one of the calibration
  campaigns ...` when several are referenced).
- BREAKING: plain `method: sobol` remains a noninferential demonstration (the
  generated `03-sobol.yml`); its inference status is renamed
  `legacy_noninferential` -> `demonstration_noninferential`, and "historical
  Sobol" is now "demonstration Sobol" in the documentation.
- Rerunning `init` on a project, even after plans, runs, analyses, and
  figures, returns `unchanged`. Only the values `init` fills in a starter are
  compared, textually (`model.root`, `model.config`, `model.data_manifest`,
  `execution.scratch`, `execution.invocation`,
  `execution.machine_options.data_path`, `campaign`, `campaign_root`); a
  difference is refused with `init inputs changed: <key> in configs/<file>`.
  Otherwise an edited starter or README is kept and listed in the new `kept`
  field; a missing one is recreated, once every check has passed, and listed
  in `restored`. A rerun accepts the starters' campaign folders and their
  content (archives following the outputs of the starters as they are on
  disk, `figures/`), `configs/analyses/` (files and subfolders, no symbolic
  links, content not read), and the `collected/` folder a failed run leaves in
  its scratch attempt; any other entry in `configs/` or `campaigns/` is refused, for
  example `foreign content in init campaign <campaign>: <entry>`.
  `--regenerate-data-manifest` passes the same checks, then replaces only
  `manifest.json`.
- `init`: on Windows, a model, data, or scratch folder on another drive is
  written as an absolute path in the starters. The generated README names the
  actual `--model-config`, states the starters' `workers: 2`, and explains
  which machine-specific values a rerun compares and how to move a project to
  another machine. The comment above the machine block of each starter says
  which values `init` fills in, and that `workers` and
  `machine_options.processes` may be changed freely.
- Worker layout shortened to `<scratch>/esa/<campaign>/w<slot>/c<12 hex>/`.
  Windows reserved names, trailing dots or spaces, case-only collisions, and
  campaign names over 64 characters are refused on every operating system.
- `run` refuses to resume a campaign whose completed runs carry different
  execution evidence. `same_machine` names the environment variables added or
  removed since the first runs (names only, never values), or says `same
  variable names, some values differ`.
- `VIRTUAL_ENV` (eqasim-sa's own environment) is no longer passed to eqasim,
  the synpp probe, analysis scripts, or the analysis interpreter probe, and is
  no longer part of the `same_machine` environment identity, so uv no longer
  warns that it does not match eqasim's `.venv`.
- Clearer errors:
  - a missing file is named instead of a raw `[Errno 2]` error by every
    command that reads a meta-configuration: `meta-config not found: <path>`
    (as given on the command line), `model.config not found: <value> (a
    relative path is read from model.root)`, and likewise `model.root`,
    `model.data_manifest`, `model.lockfile`, and `calibration meta-config`;
  - a meta-configuration, a calibration meta-configuration, or an eqasim
    configuration that is not valid YAML stops with `cannot parse meta-config
    <path>: <error>` (or `model.config`, `calibration meta-config`), and
    `init` with `init cannot parse configs/<file>: <error>`, the error giving
    its line, column, and cause (`ParserError at line 9, column 11: expected
    ',' or ']', but got '<stream end>'`);
  - `check` and `estimate` before `plan` say `campaign <campaign> has no plan
    yet: run plan first`.
- BREAKING: errors about a declared output say `output '<name>'` instead of
  `artifact '<name>'` (for example `output 'age' is missing declared column
  'x'`), and archive integrity errors say `archived file` instead of
  `archive artifact`.
- The internal `fake-run` and `eqasim-run` commands no longer appear in
  `eqasim-sa --help`; they are documented in the development guide only.
  `init --help` no longer mentions Nantes.

### Removed

- BREAKING: all backward compatibility with earlier formats: legacy scientific
  plans (`canonical_morris` and `multiscramble_sobol` design v1 and v2; only
  design v3 is read); plans, markers, and archives that identify eqasim by its
  commit (`eqasimCommit`, `modelCommit`) or archive outputs as Parquet;
  completion markers without a version or at v1 (only v2 markers bound to an
  archive manifest); execution evidence schema v1; the SLURM active-job
  journal v1 and the `<campaign>/.submitit` directory; the
  `.eqasim-sa-analysis` analysis state directory (only `analysis/` remains);
  flat analysis outputs at the campaign root; analysis manifest v6 (only
  immutable generations are read); the tolerance of `init` for `.submitit`,
  `.eqasim-sa-tmp`, and `.eqasim-sa-run-locks` in a campaign; and the
  `charter.legacy` flag (a missing charter still means a demonstration
  charter).
- `verify_release_wheel.py` at the repository root (now
  `.github/scripts/verify_release_wheel.py`, run by the release workflow) and
  the release-evidence and platform-verification pages.
- BREAKING: `method: morris` ("historical Morris") and its
  `morris-ranking.parquet` output; a configuration using it is refused. Use
  `discrete_oat` (adjacent-level demonstration) or `canonical_morris`
  (scientific screening).

### Fixed

- Commands could hang at exit after printing their result; Arrow reads
  (Parquet and CSV) no longer use worker threads.
- A failed `init` reports the real `elapsedSeconds`.

### Documentation

- English and French guides updated for all the changes above, including a
  guide to writing analysis scripts, the figures, the real scratch layout,
  the nine `check` items, what can be deleted and when, and the rules of an
  `init` rerun.

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
