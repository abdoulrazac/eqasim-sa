# Platform verification

Command: `uv run --extra test pytest -q` (or the CI workflow
`.github/workflows/tests.yml`, matrix Ubuntu, macOS, Windows, Python 3.13).

| Platform | Date | Result |
| --- | --- | --- |
| Linux (Ubuntu, kernel 6.8, Python 3.13.11) | 2026-11-12 | 1178 passed, 3 skipped in 177.66 s |
| Windows 10+ | - | pending: run `uv run --extra test pytest -q` or the CI workflow `.github/workflows/tests.yml` |
| macOS 13+ | - | pending: run `uv run --extra test pytest -q` or the CI workflow `.github/workflows/tests.yml` |

No Windows or macOS result is claimed here. Record each result in the table
above when it is obtained.

Linux notes: the skipped tests are the `windows_only` tests (skipped
automatically on other systems). A first full run showed two timeouts (10 s) in
`tests/test_campaign_run.py::test_real_sigint_returns_130_and_reaps_owned_descendants`
and `tests/test_distribution.py::test_installed_campaign_interrupts_resumes_then_is_noop`
(real subprocess plus SIGINT under full-suite load); both passed when rerun alone
and the second full run was entirely green.

## Tests to watch on Windows

- Tests marked `windows_only` (for example in `tests/test_platform.py` and
  `tests/test_eqasim_adapter.py`): Job Object process-tree termination, locks via
  `msvcrt`, junction handling, the `LongPathsEnabled` read, reserved executable
  names.
- `tests/test_process_tree.py`: the eqasim child must start in its own process
  group inside a Job Object, and Ctrl+C handling must leave no descendant.
- `tests/test_platform.py`: locks, atomic replace with bounded retries, links and
  junctions, fsync, long-path behavior.
- `tests/test_fake_run_e2e.py` and
  `test_real_ctrl_break_interrupts_and_leaves_no_descendants` (Windows
  counterpart of the POSIX-only real-SIGINT tests): end-to-end run and
  interruption with no surviving descendant.
- `tests/test_slurm_backend.py` is skipped as a module on Windows, and
  `linux_only` slurm-backend campaign tests are skipped on Windows and macOS:
  confirm the skip reasons.
- Symbolic-link tests are skipped when link creation is not permitted (no
  developer mode): confirm the skip reasons, not failures.

On macOS, watch the tests involving `/tmp` and `/var/folders` symlinked ancestors
(`/private/...`) and the 1024-character path limit.
