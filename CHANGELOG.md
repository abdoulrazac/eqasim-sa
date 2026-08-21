# Changelog

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
