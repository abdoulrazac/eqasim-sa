# Release compatibility

**English** | [Francais](../docs-fr/compatibility.md)

This page describes `eqasim-sa` 0.3.0. The Python parser and analysis code are
authoritative. Compatibility means that historical provenance remains readable;
it does not promote historical evidence to a scientific campaign and does not
migrate it.

## Plan and method matrix

| Method | Accepted charter | Objective and stochastic contract | Plan schema | Method metadata | Release meaning |
| --- | --- | --- | --- | --- | --- |
| `grid` | demonstration, including an omitted legacy charter | none; seed is a replication | v1 | none | demonstration only |
| `morris` | demonstration only | adjacent-level OAT; seed is a replication | v1 | `morrisDesignVersion` v1 | historical demonstration provenance |
| `discrete_oat` | demonstration only | local normalized adjacent-level effects; seed is a replication | v1 | `discreteOatDesignVersion` v1 | current demonstration screening only; not canonical Morris |
| `sobol` | demonstration, including an omitted legacy charter | one scramble and row bootstrap; seed is a factor | v1 | `sobolDesignVersion` v1 | readable legacy noninferential evidence |
| `canonical_morris` | scientific only | `screening`; `conditional_mean`; CRN within trajectory; declared pilot or production replication allocation | v2 | `canonicalMorrisDesignVersion` v1 | scientific screening subject to budget, stopping, and robustness |
| `multiscramble_sobol` | scientific only | `fp` or `ff`; `finite_seed_factor`; independent dependence; at least four explicit independent scrambles | v2 | `multiScrambleSobolDesignVersion` v1 | scientific FP/FF subject to budget, stopping, and robustness |

Both plan families use `canonicalizationVersion` v1. The scientific
`canonical_morris` and `multiscramble_sobol` analyzers strictly reconstruct the
sealed plan metadata, context, and design. Historical method analyzers validate
their method-specific contracts where implemented. Grid analysis instead relies
on the declared method and the expected run-marker identities and completeness;
it does not reconstruct or scientifically validate complete plan metadata. There
is no in-place or semantic migration between plan versions.

## Analysis and claim matrix

Every analysis writes `analysis-generation.json` version 6 and
`governed-report.json` version 1.

| State | `inferenceStatus` | Convergence evidence | Claim behavior |
| --- | --- | --- | --- |
| Grid, historical Morris, or discrete OAT | `demonstration_noninferential` | absent | `claimAuthorized: false` |
| Historical single-scramble Sobol | `legacy_noninferential` | absent | `claimAuthorized: false` |
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
| `morris`, `discrete_oat`, or `sobol` with a scientific charter | rejected; these methods require `charter.status: demonstration` |
| `canonical_morris` or `multiscramble_sobol` without a scientific charter | rejected; these methods require `charter.status: scientific` |
| Canonical Morris with an objective other than `screening` | rejected |
| Canonical Morris without `conditional_mean` or without independent parameter dependence | rejected |
| Multi-scramble Sobol without objective `fp`/`ff`, `finite_seed_factor`, or independent parameter dependence | rejected |
| FF tolerance outside the dimensionless interval `[0,1]` | rejected |
| Missing, unsupported, noncanonical, tampered, or context-incompatible sealed scientific plan metadata | rejected before analysis publication by the canonical Morris and multi-scramble Sobol analyzers |
| Grid plan presented for analysis | checked by method plus expected run-marker identity and completeness; Grid analysis provides no scientific metadata-validation claim |
| Scientific positions above the declared budget after reserve | rejected before plan publication |
| Incomplete OAT or Sobol analysis | rejected because completeness must equal `1.0` |
| Unsupported or unevaluable stopping evidence | fails closed as not converged |

## Init and Nantes boundaries

`eqasim-sa init` generates only calibration, Grid, discrete OAT, and historical
single-scramble Sobol demonstration starters. It generates no canonical Morris
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
| 0.3.0 | `>=3.13,<3.14` | v1 for demonstration/legacy methods; v2 for current scientific methods, with each v2 design metadata family at v1 | v6 | none |

See [release evidence](release-evidence-0.3.0.md) for commands actually run,
artifact hashes, and environment limitations.
