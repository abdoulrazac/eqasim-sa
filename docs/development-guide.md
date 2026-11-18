# Development Guide

[Français](../docs-fr/development-guide.md)

## Architecture

The package follows a ports, core, and adapters separation:

```text
eqasim-sa/
├── pyproject.toml
├── uv.lock
├── src/eqasim_sa/
│   ├── cli.py
│   ├── ports/
│   │   ├── backend.py
│   │   └── simulator.py
│   ├── core/
│   │   ├── plan.py
│   │   ├── run.py
│   │   ├── ledger.py
│   │   ├── campaign_snapshot.py
│   │   ├── analysis_publication.py
│   │   ├── analyze.py
│   │   └── indicators/
│   ├── adapters/
│   │   ├── application.py
│   │   ├── meta_config.py
│   │   ├── data_manifest.py
│   │   ├── worker.py
│   │   ├── backends/
│   │   ├── eqasim/       # adapter, code fingerprint, output files, worker workspace
│   │   └── fake/
│   └── templates/
└── tests/
```

`ports/` defines the backend and simulator protocols. `core/` owns identities,
designs, the ledger, and analyses without knowing about the CLI. `adapters/`
loads YAML, fingerprints the eqasim code, invokes synpp, copies the declared
output files, manages local/SLURM execution,
and wires the application together. `cli.py` is limited to parsing and JSON
envelopes.

Filesystem responsibilities are deliberately deep: `utils/filesystem.py`
owns stable capture and crash-conscious file commits;
`campaign_snapshot.py` owns coherent plan/run/archive cuts;
`analysis_publication.py` owns immutable analysis generations; and
`adapters/eqasim/workspace.py` owns disposable worker slots and contextual
caches. Callers do not reproduce temporary-name, descriptor, rollback, or
workspace-inventory logic.

```mermaid
flowchart TD
    CLI[cli.py] --> APP[adapters/application.py]
    APP --> META[adapters/meta_config.py]
    APP --> CORE[core plan/run/analyze/ledger]
    APP --> LOCAL[local backend]
    APP --> SLURM[SLURM backend]
    CORE --> PORTS[ports]
    EQ[eqasim adapter] --> PORTS
    FAKE[fake adapter] --> PORTS
```

## Declared dependencies

| Item | Version/constraint |
| --- | --- |
| Python | `>=3.13,<3.14` |
| pyarrow | `23.0.1` |
| PyYAML | `6.0.3` |
| submitit | `1.5.4` |
| numpy | `2.3.5` |
| pandas | `2.3.3` |
| matplotlib | `3.10.8` |
| scipy | `1.17.1` |
| SALib | `1.5.2` |
| pytest, optional dependency in the `test` extra | `9.0.2` |
| setuptools, build-system requirement only | `>=80` |

The eight libraries between pyarrow and SALib are runtime dependencies. Pandas
and Matplotlib are declared directly and pinned because SALib imports them at
runtime; this prevents transitive resolver drift from changing the numerical
environment. `pytest` is
installed only with the `test` extra; `setuptools>=80` is used to build the
package through `setuptools.build_meta` and is not declared as a runtime
dependency. The package does not install synpp or eqasim. Current scientific
Morris and Sobol designs and analyses use the declared SALib boundary.

## Install and test

From `eqasim-sa/`:

```bash
uv sync --extra test --locked
uv lock --check
uv run --locked pytest
```

Useful targeted tests:

```bash
uv run --locked pytest tests/test_init.py
uv run --locked pytest tests/test_plan.py tests/test_check.py
uv run --locked pytest tests/test_analyze.py tests/test_distribution.py
```

Check the installed commands:

```bash
uv run --locked eqasim-sa --help
uv run --locked eqasim-sa init --help
uv run --locked eqasim-sa-fake-pipeline --help
```

Build distributions from the current source:

```bash
uv build
```

Inspect and test the newly produced artifacts; do not assume that pre-existing
`build/` or `dist/` directories reflect the current source.

## Observed testing strategy

- Unit tests for identities, plans, the ledger, indicators, and statistics.
- Adversarial validation tests for Parquet plan and marker schemas, CSV output reading, paths, symbolic links, and atomic writes.
- CLI integration tests with a synpp spy executable.
- A fake pipeline for end-to-end runs and analyses.
- Metadata tests and commands from a built installation.
- The SLURM backend tested with Submitit doubles and reload contracts.
- Interruption, resumption, idempotency, and concurrency tests.

The SLURM tests do not demonstrate operation on a real cluster. The CI workflow
`.github/workflows/tests.yml` runs the suite on Ubuntu, macOS, and Windows; record
results in [Platform verification](platform-verification.md). Platform test
markers: `posix_only`, `windows_only`, and `linux_only` (slurm backend) are skipped
automatically elsewhere.

Durability note: on macOS, `os.fsync` flushes to the drive but does not force
`F_FULLFSYNC`, so a power loss may still lose the most recent writes held in the
drive cache. eqasim-sa does not issue `F_FULLFSYNC`; atomic publication (complete
or absent) is unaffected.

## Important invariants

- eqasim-sa never calls Git. The eqasim code fingerprint
  (`adapters/eqasim/source_fingerprint.py`) and the bytes of the model YAML are
  sealed in the context and checked again before every run.
- synpp receives the `run:` list of the model YAML unchanged.
- Declared output files are archived byte for byte; only their name, `file`, and
  `separator` are sealed in the plan.
- The plan is reproducible and immutable under a given root.
- The run marker is published after the archives and is the sole proof of completion.
- Plans and run markers are no-clobber publications; an existing marker is never replaced.
- Durable writes are atomic, and symbolic links are broadly rejected.
- Durable campaign coordination is confined to `.eqasim-sa/`; disposable attempts
  and contextual caches are confined to scratch.
- Machine options (`execution.machine_options`) do not modify `runId`.
- Named structured factors remain a separate label-to-list mapping across the
  adapter/core boundary: plans expose labels, resolution and `runId` use lists,
  and schema v3 seals the canonical mapping.
- Only scientific design v3 is accepted; earlier designs are rejected during
  application preflight.
- An analysis must not mutate its sources; the code backs them up and restores them if they differ.

## Add an indicator kind

The indicator kinds are `tv_distance`, `share`, and `value`, all built in. A user who needs another quantity computes it in an
eqasim analysis stage, declares the CSV file under `outputs`, and reads it with
`value`; that code is then covered by the eqasim code fingerprint.

A new built-in kind is a change to eqasim-sa itself. Implement it in
`core/indicators/` (computation, canonical definition, and `KIND_VERSIONS`),
parse it in `adapters/meta_config.py` (`INDICATOR_KINDS`), and document it in
both YAML references. Changing the bytes of an existing definition requires a
new `eqasim-sa:indicator-definition` domain version. Add tests for parsing,
errors, computation on CSV text columns, and source purity.

## Distribution caveats

Setuptools discovers packages under `src` and includes `templates/*.yml` and
`templates/*.md`. The installed scripts are `eqasim-sa` and
`eqasim-sa-fake-pipeline`. The declared Python support is intentionally narrow;
do not broaden it in the documentation without changing and testing the package
contract.
