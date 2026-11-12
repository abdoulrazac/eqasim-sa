# Changelog

## Unreleased

- Runs natively on Windows 10+, macOS 13+, and Linux (Python 3.13 via uv, no
  administrator rights) with the `local` backend; the `slurm` backend remains
  Linux only and is refused early by `check` and `run` elsewhere.
- Adds a platform layer (locks, atomic replace, fsync, links, process-tree
  supervision with a Windows Job Object) and path-based durable I/O; all text I/O
  is explicit UTF-8.
- Shortens the worker layout to `<scratch>/esa/<campaign>/w<slot>/c<12 hex>/`;
  refuses Windows reserved names, trailing dots/spaces, case-only collisions, and
  campaign names over 64 characters on every OS.
- Adds `check` items `backend`, `path_length` (reads `LongPathsEnabled` read-only
  on Windows), and `same_machine`; `run` refuses to resume a campaign whose
  completed runs carry different execution evidence.
- Makes identities line-ending independent (CRLF normalized to LF for the
  lockfile, data manifest canonical JSON, software, indicator, and plugin
  identities, and the `model.config` Git check); `init` writes a
  `.gitattributes`.
- Bumps the context, software identity, indicator definition, and plugin
  distribution hash domains to `v2` (no backward compatibility; legacy v1 plans
  remain descriptive only).
- Adds a multi-OS CI workflow and `docs/platform-verification.md`.

- Declares the project under GPL-2.0-only and adds bilingual contribution
  guidance and package README metadata.
- Adds generic named structured-list factors for Grid, historical Sobol, and
  multi-scramble Sobol. Plans store readable labels while resolved ordered lists
  determine `runId` and execution snapshots; aliases and malformed payloads are
  rejected.
- Adds plan schema v3 only when a plan seals canonical
  `structuredFactorsVersion: 1` and `structuredFactors` metadata. Unstructured
  plans retain their existing v1/v2 identities and byte behavior.
- Refuses structured factors for historical Morris, discrete OAT, and canonical
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
