"""Define the command-line interface and its JSON response envelope."""

import argparse
import importlib.metadata
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from eqasim_sa.core.run import RunSpec, run_one

EQASIM_PARAMETERS = {"sampling_rate", "hts", "income_assignation_method"}
PUBLIC_COMMANDS = (
    "init",
    "show-config",
    "plan",
    "run",
    "estimate",
    "check",
    "analyze",
    "plot",
)


def installed_version() -> str:
    """The installed eqasim-sa version, or ``unknown`` without package metadata."""

    try:
        return importlib.metadata.version("eqasim-sa")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class _VersionAction(argparse.Action):
    """Like argparse's version action, but reads the version only when used."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None):  # type: ignore[no-untyped-def]
        super().__init__(option_strings, dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        print(f"{parser.prog} {installed_version()}")
        parser.exit()


def emit(payload: dict) -> None:
    """Print one JSON response: a single line with sorted keys, as every
    command does."""

    print(json.dumps(payload, sort_keys=True, allow_nan=False))


def parser() -> argparse.ArgumentParser:
    """Build the argument parser for every installed command."""

    result = argparse.ArgumentParser(prog="eqasim-sa")
    result.add_argument(
        "--version", action=_VersionAction, help="print the installed version and exit"
    )
    # The internal commands fake-run and eqasim-run are left out of the usage
    # line (explicit metavar) and of the command list in --help (they are added
    # without help=, so argparse lists no entry for them).
    commands = result.add_subparsers(
        dest="command", metavar="{" + ",".join(PUBLIC_COMMANDS) + "}"
    )
    init = commands.add_parser(
        "init",
        help="create a complete sensitivity-analysis project",
        description=(
            "Create a sensitivity-analysis project for an eqasim folder: starter "
            "meta-configurations, a README, and the data manifest, without "
            "planning or executing any campaign."
        ),
        epilog=(
            "Example: eqasim-sa init --project-name my-study --model-root eqasim-france "
            "--data-path eqasim-data --scratch /tmp/eqasim-scratch. "
            "The project-local manifest hashes the complete data tree. "
            "Repeat --invocation once per command argument when overriding the default."
        ),
    )
    init.add_argument(
        "--project-name", required=True, metavar="NAME",
        help="safe project and campaign prefix (1 to 64 characters)",
    )
    init.add_argument(
        "--directory", type=Path, metavar="PATH",
        help="project output directory (default: ./NAME)",
    )
    init.add_argument(
        "--model-root", type=Path, default=Path("eqasim-france"), metavar="PATH",
        help="eqasim-france folder (default: ./eqasim-france)",
    )
    init.add_argument(
        "--model-config", type=Path, default=Path("config.yml"), metavar="FILE",
        help="eqasim config, relative to --model-root or absolute (default: config.yml)",
    )
    init.add_argument(
        "--regenerate-data-manifest",
        action="store_true",
        help="rebuild and atomically replace <directory>/manifest.json",
    )
    init.add_argument(
        "--data-path", type=Path, default=Path("eqasim-data"), metavar="PATH",
        help="existing eqasim input-data directory (default: ./eqasim-data)",
    )
    init.add_argument(
        "--scratch", type=Path, metavar="PATH",
        help="local scratch directory, created when absent (default: <directory>/scratch)",
    )
    init.add_argument(
        "--invocation", action="append", default=None, metavar="ARG",
        help=(
            "one argument of the synpp command; repeat in argv order "
            "(default: uv run --locked python -m synpp)"
        ),
    )
    show = commands.add_parser(
        "show-config",
        help="show the complete meta-config with defaults applied",
    )
    show.add_argument("meta_config", type=Path)
    plan = commands.add_parser("plan", help="seal a deterministic experiment plan")
    plan.add_argument("--meta-config", type=Path, required=True)
    run = commands.add_parser("run", help="execute every unique run in a sealed plan")
    run.add_argument("--meta-config", type=Path, required=True)
    estimate = commands.add_parser(
        "estimate", help="estimate the compute time and disk space of a planned campaign"
    )
    estimate.add_argument("--meta-config", type=Path, required=True)
    estimate.add_argument(
        "--calibration-meta-config", type=Path, action="append", default=[]
    )
    check = commands.add_parser("check", help="check a planned campaign before running it")
    check.add_argument("--meta-config", type=Path, required=True)
    check.add_argument(
        "--calibration-meta-config", type=Path, action="append", default=[]
    )
    check.add_argument(
        "--smoke",
        action="store_true",
        help="also execute two temporary real eqasim runs (sampling_rate 0.001)",
    )
    analyze = commands.add_parser(
        "analyze", help="compute indicators and sensitivity results from run archives"
    )
    analyze.add_argument("--meta-config", type=Path, required=True)
    analyze.add_argument(
        "--descriptive-only",
        action="store_true",
        help="publish non-inferential descriptive outputs from completed runs",
    )
    plot = commands.add_parser(
        "plot", help="draw the figures valid for the published analysis"
    )
    plot.add_argument("--meta-config", type=Path, required=True)
    plot.add_argument(
        "--figure",
        action="append",
        default=[],
        metavar="NAME",
        help="draw only this figure; repeat for several (default: every valid figure)",
    )
    fake = commands.add_parser("fake-run")
    fake.add_argument("--campaign-root", type=Path, required=True)
    fake.add_argument("--campaign", required=True)
    fake.add_argument("--config", type=Path, required=True)
    fake.add_argument("--seed", type=int, required=True)
    fake.add_argument("--expected-context-hash", required=True)
    fake.add_argument("--current-context-hash", required=True)
    fake.add_argument("--workdir", type=Path, required=True)
    eqasim = commands.add_parser("eqasim-run")
    eqasim.add_argument("--campaign-root", type=Path, required=True)
    eqasim.add_argument("--campaign", required=True)
    eqasim.add_argument("--config-yaml", type=Path, required=True)
    eqasim.add_argument("--eqasim-root", type=Path, required=True)
    eqasim.add_argument("--invocation-json", required=True)
    eqasim.add_argument("--scratch", type=Path, required=True)
    eqasim.add_argument("--seed", type=int, required=True)
    eqasim.add_argument("--expected-context-hash", required=True)
    eqasim.add_argument("--current-context-hash", required=True)
    eqasim.add_argument(
        "--outputs-json",
        required=True,
        help='declared outputs as {"name": {"file": ..., "separator": ...}}',
    )
    return result


def dispatch(
    simulator: Any,
    backend: Any,
    eqasim_factory: Any = None,
    plan_factory: Any = None,
    run_factory: Any = None,
    estimate_factory: Any = None,
    check_factory: Any = None,
    init_factory: Any = None,
    analyze_factory: Any = None,
    show_config_factory: Any = None,
    plot_factory: Any = None,
) -> int:
    """Dispatch one parsed command and emit its stable JSON response."""

    arguments = parser().parse_args()
    if arguments.command is None:
        parser().print_help()
        return 0
    if arguments.command == "show-config":
        try:
            if show_config_factory is None:
                raise RuntimeError("show-config adapter is not configured")
            text = show_config_factory(arguments.meta_config)
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is None:
                print(text, end="")
            else:
                sys.stdout.flush()
                buffer.write(text.encode("utf-8"))
                buffer.flush()
            return 0
        except Exception as error:
            print(
                str(error).strip() or error.__class__.__name__, file=sys.stderr
            )
            return 1
    if arguments.command == "init":
        started = time.monotonic()
        empty = {
            "status": "failed",
            "initVersion": 3,
            "projectName": arguments.project_name,
            "manifest": None,
            "readme": None,
            "metaConfigs": {
                method: None
                for method in ("calibration", "grid", "discrete_oat", "sobol")
            },
            "restored": [],
            "kept": [],
            "elapsedSeconds": 0.0,
            "error": None,
        }
        try:
            if init_factory is None:
                raise RuntimeError("init adapter is not configured")
            invocation = tuple(
                arguments.invocation
                or [
                    "uv", "run", "--locked", "python", "-m", "synpp",
                ]
            )
            payload = init_factory(
                arguments.project_name,
                arguments.directory or Path(arguments.project_name),
                arguments.model_root,
                arguments.data_path,
                arguments.scratch,
                invocation,
                arguments.model_config,
                arguments.regenerate_data_manifest,
            )
            emit(payload)
            return 0
        except (Exception, KeyboardInterrupt) as error:
            message = re.sub(
                r"(?<![A-Za-z0-9_.-])/(?:[^\s:;,]+)",
                "<path>",
                str(error).strip() or error.__class__.__name__,
            )
            failed = {
                **empty,
                "elapsedSeconds": time.monotonic() - started,
                "error": message,
            }
            emit(failed)
            return 130 if isinstance(error, KeyboardInterrupt) else 1
    if arguments.command == "plan":
        try:
            if plan_factory is None:
                raise RuntimeError("plan adapter is not configured")
            result = plan_factory(arguments.meta_config)
            payload = {
                "status": result.status,
                "path": str(result.path),
                "designPositions": result.design_positions,
                "uniqueRuns": result.unique_runs,
                "contextHash": result.context_hash,
                "error": None,
            }
            emit(payload)
            return 0
        except Exception as error:
            message = str(error).strip() or error.__class__.__name__
            emit(
                {
                    "status": "failed",
                    "path": None,
                    "designPositions": 0,
                    "uniqueRuns": 0,
                    "contextHash": None,
                    "error": message,
                }
            )
            return 1
    if arguments.command == "run":
        empty = {
            "status": "failed",
            "total": 0,
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "failures": [],
        }
        try:
            if run_factory is None:
                raise RuntimeError("run adapter is not configured")
            payload = run_factory(arguments.meta_config)
            emit(payload)
            return 0 if payload["status"] == "completed" else 1
        except Exception as error:
            message = str(error).strip() or error.__class__.__name__
            emit({**empty, "error": message})
            return 1
    if arguments.command == "estimate":
        empty = {
            "status": "failed",
            "estimateVersion": 2,
            "method": None,
            "formula": None,
            "formulaInputs": None,
            "designPositions": None,
            "runs": None,
            "calibrationRuns": None,
            "meanDurationSeconds": None,
            "meanDurableBytesPerRun": None,
            "estimatedComputeHours": None,
            "estimatedDiskBytes": None,
            "workers": None,
            "observedScratchBytesPerWorker": None,
            "estimatedScratchBytes": None,
        }
        try:
            if estimate_factory is None:
                raise RuntimeError("estimate adapter is not configured")
            payload = estimate_factory(
                arguments.meta_config, tuple(arguments.calibration_meta_config)
            )
            emit(payload)
            return 0
        except Exception as error:
            message = str(error).strip() or error.__class__.__name__
            emit({**empty, "error": message})
            return 1
    if arguments.command == "check":
        try:
            if check_factory is None:
                raise RuntimeError("check adapter is not configured")
            payload = check_factory(
                arguments.meta_config,
                tuple(arguments.calibration_meta_config),
                arguments.smoke,
            )
        except Exception as error:
            message = re.sub(
                r"(?<![A-Za-z0-9_.-])/(?:[^\s:;,]+)",
                "<path>",
                str(error).strip() or error.__class__.__name__,
            )
            payload = {
                "status": "invalid",
                "checkVersion": 2,
                "elapsedSeconds": 0.0,
                "checks": [],
                "estimatedDiskBytes": None,
                "requiredDiskBytes": None,
                "freeDiskBytes": None,
                "missingDiskBytes": None,
                "estimatedScratchBytes": None,
                "freeScratchBytes": None,
                "missingScratchBytes": None,
                "error": message,
            }
        emit(payload)
        return 0 if payload["status"] == "valid" else 1
    if arguments.command == "analyze":
        empty = {
            "status": "failed",
            "path": None,
            "runs": 0,
            "indicators": 0,
            "values": 0,
            "completionRate": None,
            "expected": 0,
            "completed": 0,
            "missing": 0,
            "failed": 0,
            "dispersionPath": None,
            "sensitivityMethod": None,
            "sensitivityPath": None,
            "convergencePath": None,
            "rankReversalsPath": None,
            "byScramblePath": None,
            "diagnosticsPath": None,
            "noiseFloorPath": None,
            "replicationAllocationPath": None,
            "outputSummaryPath": None,
            "convergenceEvidencePath": None,
            "governedReportPath": None,
            "inferenceStatus": None,
            "analyses": {},
            "warnings": [],
        }
        try:
            if analyze_factory is None:
                raise RuntimeError("analyze adapter is not configured")
            payload = (
                analyze_factory(arguments.meta_config, descriptive_only=True)
                if arguments.descriptive_only
                else analyze_factory(arguments.meta_config)
            )
            emit(payload)
            return 0
        except (Exception, KeyboardInterrupt) as error:
            message = str(error).strip() or error.__class__.__name__
            emit({**empty, "error": message})
            return 130 if isinstance(error, KeyboardInterrupt) else 1
    if arguments.command == "plot":
        try:
            if plot_factory is None:
                raise RuntimeError("plot adapter is not configured")
            payload = plot_factory(arguments.meta_config, tuple(arguments.figure))
            emit(payload)
            return 0
        except (Exception, KeyboardInterrupt) as error:
            print(
                str(error).strip() or error.__class__.__name__, file=sys.stderr
            )
            return 130 if isinstance(error, KeyboardInterrupt) else 1
    try:
        if arguments.command == "fake-run":
            config = json.loads(arguments.config.read_text(encoding="utf-8"))
            workdir = arguments.workdir
        else:
            if eqasim_factory is None:
                raise RuntimeError("eqasim adapter is not configured")
            simulator, config, workdir = eqasim_factory(arguments)
        if not isinstance(config, dict):
            raise TypeError("configuration root must be an object")
        nested = config.get("config", {})
        if not isinstance(nested, dict):
            raise TypeError("configuration block 'config' must be an object")
    except Exception as error:
        emit({"runId": "", "status": "failed", "error": str(error)})
        return 1
    retained_parameters = (
        nested.keys() if arguments.command == "fake-run" else EQASIM_PARAMETERS
    )
    parameters = {
        key: value
        for key, value in nested.items()
        if key in retained_parameters and isinstance(value, (bool, int, float, str))
    }
    spec = RunSpec(
        arguments.campaign,
        config,
        arguments.seed,
        arguments.expected_context_hash,
        parameters,
    )
    result = run_one(
        spec,
        simulator,
        backend,
        arguments.campaign_root,
        workdir,
        arguments.current_context_hash,
    )
    emit(
        {
            "runId": result.run_id,
            "status": result.status,
            "error": result.error,
        }
    )
    return 0 if result.status in {"completed", "skipped"} else 1
