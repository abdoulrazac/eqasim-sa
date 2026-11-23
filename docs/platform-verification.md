# Platform verification

Command: `uv run --extra test pytest -q` (or the CI workflow
`.github/workflows/tests.yml`, matrix Ubuntu, macOS, Windows, Python 3.13).

| Platform | Date | Result |
| --- | --- | --- |
| Linux (Ubuntu, kernel 6.8, Python 3.13.11) | 2026-11-23 | 1557 passed, 4 skipped in 205.02 s |
| Windows 10+ | - | pending: run `uv run --extra test pytest -q` or the CI workflow `.github/workflows/tests.yml` |
| macOS 13+ | - | pending: run `uv run --extra test pytest -q` or the CI workflow `.github/workflows/tests.yml` |

No Windows or macOS result is claimed here. Record each result in the table
above when it is obtained.

Linux notes: three skipped tests are the `windows_only` tests (skipped
automatically on other systems); the fourth,
`tests/test_eqasim_adapter.py::test_probe_on_the_real_synpp`, is skipped because
synpp is not installed in the eqasim-sa environment. The run was made with
`-p no:cacheprovider` and was green on the first attempt. The real-SIGINT tests
(`tests/test_campaign_run.py::test_real_sigint_returns_130_and_reaps_owned_descendants`,
`tests/test_distribution.py::test_installed_campaign_interrupts_resumes_then_is_noop`)
start real subprocesses with 10 s timeouts and can time out under heavy machine
load; rerun them alone before suspecting a regression.

## Tests to watch on Windows

- The three tests marked `windows_only`, which run only on Windows:
  `tests/test_platform.py::test_is_link_detects_junction`,
  `tests/test_eqasim_adapter.py::test_resolve_invocation_finds_exe_without_suffix`,
  and `tests/test_campaign_run.py::test_real_ctrl_break_interrupts_and_leaves_no_descendants`.
  Other Windows code paths (Job Object process-tree termination, locks via
  `msvcrt`, the `LongPathsEnabled` read, Windows reserved file names) are
  exercised on every system with simulated Windows APIs.
- `tests/test_process_tree.py`: the eqasim child must start in its own process
  group inside a Job Object, and Ctrl+C handling must leave no descendant.
- `tests/test_platform.py`: locks, atomic replace with bounded retries, links and
  junctions, fsync, long-path behavior.
- `tests/test_fake_run_e2e.py` and the real Ctrl+Break test above (Windows
  counterpart of the POSIX-only real-SIGINT tests): end-to-end run and
  interruption with no surviving descendant.
- `tests/test_slurm_backend.py` is skipped as a module on Windows, and
  `linux_only` slurm-backend campaign tests are skipped on Windows and macOS:
  confirm the skip reasons.
- Symbolic-link tests are skipped when link creation is not permitted (no
  developer mode): confirm the skip reasons, not failures.

On macOS, watch the tests involving `/tmp` and `/var/folders` symlinked ancestors
(`/private/...`) and the 1024-character path limit.
