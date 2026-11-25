# Release compatibility

**English** | [Français](../docs-fr/compatibility.md)

This page describes `eqasim-sa` 0.4.4; the [changelog](../CHANGELOG.md) lists
its changes since 0.3.0, among them structured factors,
an eqasim model identified without Git, analysis scripts, `show-config`, and
`plot`. The Python parser and analysis code are authoritative. There is no backward compatibility:
only the current formats are read, and nothing is migrated.

## Plan and method matrix

| Method | Accepted charter | Objective and stochastic contract | Plan schema | Method metadata | Release meaning |
| --- | --- | --- | --- | --- | --- |
| `grid` | demonstration, including an omitted charter | none; seed is a replication | v1; v3 with structured factors | none | demonstration only; recommended first for named structures |
| `discrete_oat` | demonstration only | local normalized adjacent-level effects; seed is a replication | v1 | `discreteOatDesignVersion` v1 | current demonstration screening only; not canonical Morris |
| `sobol` | demonstration, including an omitted charter | one scramble and row bootstrap; seed is a factor | v1; v3 with structured factors | `sobolDesignVersion` v1 | noninferential demonstration only |
| `canonical_morris` | scientific only | `screening`; `conditional_mean`; CRN within trajectory; declared pilot or production replication allocation | v2 | `canonicalMorrisDesignVersion` v3 (SALib) | current v3 supports scientific screening subject to budget, stopping, and robustness |
| `multiscramble_sobol` | scientific only | `fp` or `ff`; `finite_seed_factor`; independent dependence; at least four explicit independent scrambles | v2; v3 with structured factors | `multiScrambleSobolDesignVersion` v3 (SALib) | current v3 supports scientific FP/FF subject to a defensible label distribution, budget, stopping, and robustness |

All plans use `canonicalizationVersion` v1 and carry the sealed `modelDocument`
metadata (the canonical eqasim configuration without the machine-only keys and
`random_seed`); a plan without it is refused, Grid included. Schema v3 is written only for a
structured plan and adds the exact canonical pair `structuredFactorsVersion: 1`
and `structuredFactors`. Under plan schema v3, demonstration Sobol and multi-scramble
Sobol also copy that complete label-to-payload mapping into their method design
metadata as a second sealed invariant; both canonical mappings must be exactly equal.
The plan schema version is independent from the numerical design version.
Scientific designs use version v3 (the SALib contract). Scientific v3 analyzers strictly reconstruct the
sealed plan metadata, context, and SALib design. Demonstration analyzers validate
their method-specific contracts.
Direct core Grid analysis validates schema-v3 metadata shape and labels but has no
method design metadata or model configuration with which to bind payloads. The CLI
application preflight is authoritative for that full binding: it reconstructs
every structured plan from the meta-configuration and the sealed
`modelDocument` before core analysis. There is
no in-place or semantic
migration between plan versions.

## Analysis and claim matrix

Every analysis writes `analysis-generation.json` version 8 (with
`provenance.analyses` and `provenance.analysisEnvironments`), `captured-run-set.json` schema version 2 (with
`analyses` in each run), and `governed-report.json` version 1.
For a scientific v3 analysis, `provenance.numericalEngine` is exactly the
descriptor sealed in the plan.

| State | `inferenceStatus` | Convergence evidence | Claim behavior |
| --- | --- | --- | --- |
| Grid or discrete OAT | `demonstration_noninferential` | absent | `claimAuthorized: false` |
| Demonstration single-scramble Sobol | `demonstration_noninferential` | absent | `claimAuthorized: false` |
| Canonical Morris pilot | `pilot_only` | reported, but cannot promote the pilot | `claimAuthorized: false` |
| Canonical Morris production | `conditional` if all declared stopping criteria pass; otherwise `unstable` | required and evaluated | robustness remains pending; `claimAuthorized: false` |
| Multi-scramble Sobol FP/FF | `conditional` if all declared stopping criteria pass; otherwise `unstable` | required and evaluated across independent scrambles | robustness remains pending; `claimAuthorized: false` |

Screening retains factors for later consideration; it does not estimate variance
shares or justify fixing. FP identifies which uncertainty should be learned more
accurately first. FF requires a declared practical total-order variance-share
tolerance including interactions. The finite seed factor is non-actionable and
excluded from FP/FF decision ranks.

## Refusal matrix

| Incompatible state | Refusal |
| --- | --- |
| `discrete_oat` or `sobol` with a scientific charter | rejected; these methods require `charter.status: demonstration` |
| `canonical_morris` or `multiscramble_sobol` without a scientific charter | rejected; these methods require `charter.status: scientific` |
| Canonical Morris with an objective other than `screening` | rejected |
| Canonical Morris without `conditional_mean` or without independent parameter dependence | rejected |
| Multi-scramble Sobol without objective `fp`/`ff`, `finite_seed_factor`, or independent parameter dependence | rejected |
| FF tolerance outside the dimensionless interval `[0,1]` | rejected |
| Structured `seed`, duplicate payload aliases, malformed ordered lists, or a model key that is not a string list | rejected |
| Structured factor with `discrete_oat` or `canonical_morris` | rejected because nominal label order is arbitrary |
| Missing, unsupported, noncanonical, tampered, or context-incompatible sealed scientific plan metadata | rejected before analysis publication by the canonical Morris and multi-scramble Sobol analyzers |
| Known output left at its default `file` whose eqasim stage is missing from `run:` | rejected when the meta-configuration is loaded |
| Output whose `file` suffix (after `{prefix}` is replaced) cannot name an archive file | rejected when the meta-configuration is loaded |
| Output whose file eqasim would not write because `output_formats` of the eqasim config (default `[csv, gpkg]`) lacks its format: known population `.csv` or `_parquet` output at its default file, any `.gpkg` or `.geoparquet` file, or a spatial output without `file` when neither `gpkg` nor `geoparquet` is listed | rejected when the meta-configuration is loaded |
| `legs` or `pt_legs` at their default file while `mode_choice` is not true in the eqasim config | rejected when the meta-configuration is loaded |
| `separator` declared on an output whose `file` is not a `.csv` file | rejected when the meta-configuration is loaded |
| `share`, `value`, or `tv_distance` indicator on an output whose `file` is not a `.csv` file | rejected when the meta-configuration is loaded |
| Indicator entry whose key is not `tv_distance`, `share`, or `value` | rejected when the meta-configuration is loaded |
| `share.category` or a `where` value that is not a quoted YAML string | rejected when the meta-configuration is loaded |
| Grid plan presented for analysis | direct core analysis validates method, run-marker identity, completeness, and schema-v3 shape/labels; application preflight reconstructs the plan to bind payloads to the model/meta-configuration |
| Scientific positions above the declared budget after reserve | rejected before plan publication |
| Incomplete OAT or Sobol analysis | rejected because completeness must equal `1.0` |
| Unsupported or unevaluable stopping evidence | fails closed as not converged |
| Plan without the sealed `modelDocument` | rejected by `run`, `check`, `estimate`, and `analyze`: `plan.parquet has no sealed modelDocument; plan a new campaign` |
| Analysis input not declared in `outputs`, another analysis, or a file that is not `.csv`, `.gpkg`, `.parquet` or `.geoparquet` | rejected when the meta-configuration is loaded |
| Analysis script missing, not a regular `.py` file, or a symbolic link | rejected when the meta-configuration is loaded |
| Analysis named like an output (ignoring case) or `logs` | rejected when the meta-configuration is loaded |
| `analyses` with an `execution.invocation` not ending with `-m synpp` and no `execution.python` | rejected when the meta-configuration is loaded |
| `primary_indicator` reading an analysis output | rejected when the meta-configuration is loaded |
| Analysis script error for any run | the whole analysis fails, whatever `completeness_threshold` |

## Platforms and hash domains

`eqasim-sa` supports Windows 10+, macOS 13+, and Linux natively with the `local`
backend (the suite is tested on Linux, Python 3.13: 1599 passed, 4 skipped; the
GitHub Actions CI workflow `.github/workflows/tests.yml` runs it on Ubuntu,
macOS, and Windows, and no Windows or macOS result is claimed until CI has run);
`slurm` is Linux only.
Plans and their evidence use exactly the domains below:

| Domain | Current |
| --- | --- |
| Execution context (`eqasimSourceSha256`) | `eqasim-sa:context:v3` |
| eqasim code fingerprint | `eqasim-sa:model-source:v1` |
| Execution evidence (`schemaVersion: 3`, `modelSourceSha256`) | `eqasim-sa:execution-evidence:v3` |
| `eqasim-sa` software identity | `eqasim-sa:software-identity:v2` |
| Indicator definition (`tv_distance`, `share`, `value`; an analysis output is described by `{name, analysis, inputs, separator}`) | `eqasim-sa:indicator-definition:v4` |
| Analysis result (analysis, script, inputs, run parameters, environment) | `eqasim-sa:analysis-result:v1` |

Text inputs are hashed after CRLF-to-LF normalization
(see [Troubleshooting](troubleshooting.md#line-endings-and-gitattributes)).
Each worker uses `<scratch>/esa/<campaign>/w<slot>/`, holding its cache
directory `c<12 hex>/` and its attempt directories `a<12 hex>/`.

## eqasim model and outputs

- `model.root` is any eqasim folder (clone, copy, or archive). Git is not
  required: the code is identified by a content fingerprint, and
  `model.config` is read relative to `model.root` (or as an absolute path).
- synpp runs the `run:` list of the eqasim configuration unchanged.
- Before the first run, eqasim-sa asks the model's interpreter for its synpp
  version and passes each override as `--config KEY VALUE` from synpp 1.7.0
  on, `--KEY VALUE` before. An `execution.invocation` that does not end with
  `-m synpp` is assumed to run synpp 1.7.0 or later.
- Each declared output is archived byte for byte as `<name><suffix>` (for
  example `age.csv`), with its original path in the run manifest. The analysis
  results (`indicators.parquet`, sensitivity tables) are Parquet files.
- The plan seals the declared outputs (`outputsDeclaration`: name, `file`,
  `separator`); `category`, `simulated`, and `references` can change between
  runs and analysis.
- The plan also seals the eqasim configuration (`modelDocument`, without the
  machine-only keys); `analyze` checks the meta-configuration against it, so
  eqasim may change after the runs without blocking analysis.
- The indicator kinds are `tv_distance`, `share`, and `value`. Custom
  calculations are [analysis scripts](analysis-scripts.md) (`analyses`), run
  by `analyze` on the archived files with eqasim's Python interpreter, or an
  eqasim analysis stage whose CSV file is read with `value`.
- `plot` draws figures from the published analysis generation into
  `figures/<generationId>/` of the campaign, with `figures.json`; the figures
  are not part of the sealed generation.

## Init and Nantes boundaries

`eqasim-sa init` generates only calibration, Grid, discrete OAT, and demonstration
single-scramble Sobol starters. It generates no canonical Morris
or multi-scramble Sobol scientific starter, no Nantes protocol, no plan, and no
simulation or analysis output.

`protocols/nantes-scientific/` is repository protocol material, not package data
and not an init template. Its renderer can produce a phase-specific
meta-configuration only after checking sealed upstream plans and evidence. It
refuses missing, symlinked, malformed, tampered, context-incompatible, or
phase-incompatible evidence without creating output. Renderer tests use fake,
temporary model and evidence fixtures. They do not establish that the real
Nantes protocol has been planned or executed.

## Release matrix

| Release | Python | Plan schemas read/written | Analysis manifest | Migration |
| --- | --- | --- | --- | --- |
| 0.4.4 | `>=3.13,<3.14` | plan schemas v1/v2/v3; scientific design v3 only | v8 | none |
| 0.4.3 | `>=3.13,<3.14` | plan schemas v1/v2/v3; scientific design v3 only | v8 | none |
| 0.4.2 | `>=3.13,<3.14` | plan schemas v1/v2/v3; scientific design v3 only | v8 | none |
| 0.4.1 | `>=3.13,<3.14` | plan schemas v1/v2/v3; scientific design v3 only | v8 | none |
| 0.4.0 | `>=3.13,<3.14` | plan schemas v1/v2/v3; scientific design v3 only | v8 | none |
