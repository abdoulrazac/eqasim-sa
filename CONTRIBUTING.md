# Contributing to eqasim-sa

[Français](CONTRIBUTING-fr.md)

Thank you for improving `eqasim-sa`. Before starting a substantial change,
describe the problem and the proposed behavior in an issue or discussion so the
scope and scientific implications can be agreed upon.

By submitting a contribution, you agree that it may be distributed under the
project's [GNU General Public License version 2.0 only](LICENSE).

## Development setup

The project targets Python 3.13. Install the locked development environment from
the repository root:

```bash
uv sync --extra test --locked
```

Keep generated files, virtual environments, credentials, local datasets, and
campaign outputs out of commits. Do not add real eqasim data or configuration
containing private paths or secrets.

## Making a change

- Keep changes focused and add regression tests for behavior changes.
- Preserve deterministic plans, immutable published artifacts, and fail-closed
  validation at trust boundaries.
- Treat changes to plan identities, schemas, stochastic designs, estimands, or
  claim authorization as compatibility changes. Document them in both `docs/`
  and `docs-fr/` and add an entry to `CHANGELOG.md`.
- Keep the English and French user documentation aligned when behavior or
  terminology changes.

The [development guide](docs/development-guide.md) describes the architecture,
test strategy, package boundary, and project invariants in more detail.

## Validation

Run the checks that apply to your change. Before proposing a release, run the
complete baseline:

```bash
uv lock --check
uv run --locked pytest
git diff --check
uv build
uv run --locked python verify_release_wheel.py \
  dist/eqasim_sa-0.3.0-py3-none-any.whl
```

Inspect the newly built archives before publishing them. Existing files in
`build/` or `dist/` are not evidence for the current source tree.

## Proposing the change

Explain the user-visible behavior, compatibility impact, scientific assumptions,
and validation performed. Include a small reproducible example when the change
affects the CLI or a campaign configuration. Never include credentials, private
data, or machine-specific paths in an issue, patch, test fixture, or log.
