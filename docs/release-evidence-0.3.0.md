# Release evidence 0.3.0

> Note (2026-11-23): this records release 0.3.0 only and is not rerun against the current development version, whose changes (no `charter` block in the starters, new `show-config` and `plot` commands, and others) are listed under Unreleased in the [changelog](../CHANGELOG.md).

Date: 2026-08-21

All commands below were run from the `eqasim-sa` repository root.

## Locked tests and lock

```bash
uv run --locked pytest tests/test_release_contract.py
uv run --locked pytest
uv lock --check
```

Outcomes: all commands exited 0. The focused suite passed 6 tests in 2.30
seconds. The full suite passed 701 tests in 69.46 seconds. Pytest reported two
`DeprecationWarning` instances from Python 3.13 multiprocessing fork use in
`test_analyze_serializes_concurrent_publications`. The lock check resolved 14
packages without changing `uv.lock`.

## Build and wheel boundary

```bash
uv build
uv run --locked python verify_release_wheel.py \
  dist/eqasim_sa-0.3.0-py3-none-any.whl
sha256sum dist/eqasim_sa-0.3.0-py3-none-any.whl \
  dist/eqasim_sa-0.3.0.tar.gz
```

Outcomes: all commands exited 0. The build produced the 0.3.0 wheel and source
archive. The executable verifier found exactly these template resources:

- `eqasim_sa/templates/00-calibration.yml`
- `eqasim_sa/templates/01-grid.yml`
- `eqasim_sa/templates/02-discrete-oat.yml`
- `eqasim_sa/templates/03-sobol.yml`
- `eqasim_sa/templates/README.md`
- `eqasim_sa/templates/__init__.py`

It also verified that the wheel contains no path component named `protocols`,
`docs`, or `docs-fr`. Normal pytest does not require a prebuilt distribution;
its low-cost verifier test creates a temporary synthetic wheel, while the
command above is the archive-level release evidence.

```text
915378444e1e8b4180cef6fef6690d1d7f2d778973c16b7c0d5d5b72e26fffc5  dist/eqasim_sa-0.3.0-py3-none-any.whl
a8ac10492d19013c8a3d507435954c1847427a72394c3185752fdefe180c57bf  dist/eqasim_sa-0.3.0.tar.gz
```

## Recreated isolated wheel smoke

The environment was recreated with CPython 3.13.11. The current wheel was
force-reinstalled rather than reusing an editable or previous installation.

```bash
uv venv --clear --python 3.13 \
  /tmp/opencode/eqasim-sa-release-0.3.0-20260821
uv pip install --force-reinstall \
  --python /tmp/opencode/eqasim-sa-release-0.3.0-20260821/bin/python \
  dist/eqasim_sa-0.3.0-py3-none-any.whl

/tmp/opencode/eqasim-sa-release-0.3.0-20260821/bin/python -c \
  'import importlib.metadata, eqasim_sa; print(importlib.metadata.version("eqasim-sa")); print(eqasim_sa.__file__)'
/tmp/opencode/eqasim-sa-release-0.3.0-20260821/bin/eqasim-sa --help
/tmp/opencode/eqasim-sa-release-0.3.0-20260821/bin/eqasim-sa-fake-pipeline --help

FIXTURE="$(mktemp -d /tmp/opencode/eqasim-sa-release-fixture.XXXXXX)"
mkdir -p "$FIXTURE/model" "$FIXTURE/data"
cp ../eqasim-france/config_local_nantes.yml \
  "$FIXTURE/model/config_local_nantes.yml"
cp ../eqasim-france/uv.lock "$FIXTURE/model/uv.lock"
cp README.md "$FIXTURE/data/input.txt"
/tmp/opencode/eqasim-sa-release-0.3.0-20260821/bin/eqasim-sa init \
  --project-name release-smoke \
  --directory "$FIXTURE/project" \
  --model-root "$FIXTURE/model" \
  --data-path "$FIXTURE/data"

FIXTURE="$FIXTURE" \
  /tmp/opencode/eqasim-sa-release-0.3.0-20260821/bin/python -c \
  'import os; from pathlib import Path; import yaml; root=Path(os.environ["FIXTURE"])/"project"; configs=sorted((root/"configs").glob("*.yml")); assert [p.name for p in configs] == ["00-calibration.yml", "01-grid.yml", "02-discrete-oat.yml", "03-sobol.yml"]; assert all(yaml.safe_load(p.read_text())["charter"]["status"] == "demonstration" for p in configs); assert not tuple(root.rglob("plan.parquet")); assert not tuple(root.rglob("render_protocol.py")); print(f"installed init boundary verified: {root}")'

uv pip check \
  --python /tmp/opencode/eqasim-sa-release-0.3.0-20260821/bin/python
```

Outcomes: all commands in the successful sequence exited 0. Installation
reported `eqasim-sa==0.3.0` and seven dependencies. The import reported version
`0.3.0` from the isolated environment's `site-packages`. The main CLI listed
`init`, `plan`, `run`, `estimate`, `check`, `analyze`, `fake-run`, and
`eqasim-run`; the fake pipeline CLI listed required `--request` and `--manifest`
options. Installed `init` returned `status: initialized`, `initVersion: 3`, and
exactly four demonstration meta-configs. The explicit boundary assertion printed
`installed init boundary verified`. `uv pip check` checked 8 packages and found
all installed packages compatible.

Before the successful init command, an initial fixture attempt copied
`../config_local_nantes.yml`; `init` failed closed with
`config.matching_age_scheme must be explicitly matching_6` and published no
manifest or starter paths. Replacing it with the declared
`../eqasim-france/config_local_nantes.yml` fixture produced the successful result
recorded above.

## Java and Nantes limitations

```bash
java -version
update-alternatives --list java
```

Both commands exited 0. Java reported OpenJDK 17.0.19; alternatives listed Java
11 and Java 17 only. No Java 25 runtime was available, so Java 25 compatibility
was not executed.

The test suite executed fake/test-harness Nantes renderer and fail-closed gate
checks. No command planned, simulated, analyzed, or executed the real Nantes
protocol, so this evidence makes no Nantes execution claim.

## Patch checks

```bash
git diff --check
git add CHANGELOG.md docs-fr/index.md docs-fr/compatibility.md \
  docs/index.md docs/compatibility.md docs/release-evidence-0.3.0.md \
  pyproject.toml src/eqasim_sa/adapters/meta_config.py \
  tests/test_canonical_morris_analyze.py \
  tests/test_multiscramble_sobol_plan.py tests/test_release_contract.py \
  uv.lock verify_release_wheel.py
git diff --cached --check
git status --short
```

Outcomes: both diff checks exited 0 with no output, including the explicit check
of newly added files after staging. The final status was reviewed after staging
and contained only the intended release source, documentation, test, lock, and
wheel-verifier paths. Nothing was committed.
