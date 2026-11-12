import json
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.adapters import checks as check_service
from eqasim_sa.core.indicators import TableArtifact, TotalVariationIndicator
from eqasim_sa.core.plan import GridDesign, unique_config_seed_pairs
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.ports.simulator import Artifact, Manifest
from eqasim_sa.utils import platform
from tests.provenance_helpers import context_hash


def design(
    root: Path, factors: dict[str, list], seeds: tuple[int, ...] = (1,)
) -> GridDesign:
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    return GridDesign(
        {
            "working_directory": "/ignored",
            "run": [],
            "config": {"rate": 1, "sampling_rate": 1},
        },
        factors,
        seeds,
        {"run": ["stage"]},
        context_hash(descriptor),
        descriptor,
        root,
    )


def test_core_reports_repeated_canonical_configuration_seed_pairs(
    tmp_path: Path,
) -> None:
    assert not unique_config_seed_pairs(design(tmp_path, {"rate": [1, 1]}))
    assert unique_config_seed_pairs(design(tmp_path, {"rate": [1, 2]}))


@pytest.mark.parametrize("method", ["morris", "sobol"])
def test_static_check_accepts_sealed_structural_coincidences_but_grid_still_rejects_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    from eqasim_sa.adapters import application
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "plan.parquet").write_bytes(b"plan")
    meta = SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document={"config": {"rate": 0, "sampling_rate": 1}},
        factors={"rate": [0, 1]},
        seeds=(1, 2),
        method=method,
        trajectories=2,
        design_seed=7,
        context_hash="c" * 64,
        context_descriptor={"version": "test"},
        base_sample_size=2,
        execution=SimpleNamespace(invocation=("spy",), scratch=tmp_path, workers=1),
        model_root=tmp_path,
        calibration_meta_configs=(),
        indicator_requirements=(),
        software_identity=read_software_identity(),
    )
    monkeypatch.setattr(check_service, "load_meta_config", lambda *args, **kwargs: meta)
    monkeypatch.setattr(check_service, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(check_service, "unique_config_seed_pairs", lambda design: False)
    monkeypatch.setattr(
        check_service,
        "_estimate_state",
        lambda *args: ({"estimatedDiskBytes": 0}, meta, (), 0),
    )

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    duplicate = next(
        item for item in payload["checks"] if item["name"] == "uniqueConfigSeedPairs"
    )
    assert duplicate == {
        "name": "uniqueConfigSeedPairs",
        "status": "passed",
        "error": None,
    }


def test_morris_position_check_fails_when_sealed_plan_validation_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application

    root = tmp_path / "campaign"
    root.mkdir()
    (root / "plan.parquet").write_bytes(b"damaged")
    meta = SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document={"config": {"rate": 0}},
        factors={"rate": [0, 1]},
        seeds=(1,),
        method="morris",
        trajectories=1,
        design_seed=7,
        context_hash="c" * 64,
        context_descriptor={"version": "test"},
        execution=None,
        model_root=tmp_path,
        calibration_meta_configs=(),
        indicator_requirements=(),
    )
    monkeypatch.setattr(check_service, "load_meta_config", lambda *args, **kwargs: meta)
    monkeypatch.setattr(
        check_service,
        "load_run_specs",
        lambda *args: (_ for _ in ()).throw(ValueError("damaged sealed plan")),
    )

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    checks = {item["name"]: item for item in payload["checks"]}
    assert checks["sealedPlan"]["status"] == "failed"
    assert checks["uniqueConfigSeedPairs"]["status"] == "failed"
    assert "sealedPlan" in checks["uniqueConfigSeedPairs"]["error"]


def test_check_cli_has_exact_deterministic_schema(monkeypatch, capsys) -> None:
    from eqasim_sa.cli import dispatch

    payload = {
        "status": "valid",
        "checkVersion": 1,
        "elapsedSeconds": 0.1,
        "checks": [
            {"name": name, "status": "passed", "error": None}
            for name in (
                "metaConfig",
                "sealedPlan",
                "uniqueConfigSeedPairs",
                "diskSpace",
            )
        ],
        "estimatedDiskBytes": 10,
        "requiredDiskBytes": 4,
        "freeDiskBytes": 5,
        "missingDiskBytes": 0,
        "error": None,
    }
    seen = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eqasim-sa",
            "check",
            "--meta-config",
            "target.yml",
            "--calibration-meta-config",
            "cal.yml",
        ],
    )
    assert (
        dispatch(
            None,
            None,
            check_factory=lambda path, calibrations, smoke: (
                seen.append((path, calibrations, smoke)) or payload
            ),
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output == json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n"
    assert seen == [(Path("target.yml"), (Path("cal.yml"),), False)]


def test_check_aggregates_duplicate_and_disk_errors(
    tmp_path: Path, monkeypatch
) -> None:
    from eqasim_sa.adapters import application

    root = tmp_path / "campaign"
    root.mkdir()
    plan = root / "plan.parquet"
    meta = SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document=design(root, {"rate": [1]}).base_config,
        factors={"rate": [1, 1]},
        seeds=(1,),
        context_hash="c" * 64,
        context_descriptor=design(root, {"rate": [1]}).context_descriptor,
        execution=SimpleNamespace(invocation=("missing",), scratch=tmp_path, workers=1),
        model_root=tmp_path,
        model_config=tmp_path / "config.yml",
        model_bytes=b"config: {}",
        calibration_meta_configs=(),
        indicator_requirements=(),
    )
    monkeypatch.setattr(
        check_service,
        "load_meta_config",
        lambda path, validate_factors=True, deadline=None: meta,
    )
    monkeypatch.setattr(check_service, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(check_service, "unique_config_seed_pairs", lambda value: False)
    monkeypatch.setattr(
        check_service,
        "_estimate_state",
        lambda *args: (_ for _ in ()).throw(ValueError("no calibration")),
    )
    plan.write_bytes(b"plan")

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    assert payload["status"] == "invalid"
    errors = {item["name"]: item["error"] for item in payload["checks"]}
    assert "duplicate" in errors["uniqueConfigSeedPairs"]
    assert "calibration" in errors["diskSpace"]
    assert payload["elapsedSeconds"] < 60


def test_check_captures_unknown_artifact_producer_as_structured_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application
    from eqasim_sa.ports.simulator import IndicatorRequirement

    meta = SimpleNamespace(
        factors={},
        model_document={"config": {}},
        calibration_meta_configs=(),
        indicator_requirements=(IndicatorRequirement("custom(unknown)", "unknown"),),
        seeds=(1,),
        context_hash="c" * 64,
        context_descriptor={"version": "test"},
        campaign_root=tmp_path / "campaign",
    )
    monkeypatch.setattr(check_service, "load_meta_config", lambda *args, **kwargs: meta)

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    assert payload["status"] == "invalid"
    diagnostic = next(
        item for item in payload["checks"] if item["name"] == "metaConfig"
    )
    assert diagnostic["status"] == "failed"
    assert "custom(unknown)" in diagnostic["error"] and "unknown" in diagnostic["error"]


def test_check_reports_disk_measurements_when_space_is_insufficient(
    tmp_path: Path, monkeypatch
) -> None:
    from eqasim_sa.adapters import application

    root = tmp_path / "campaign"
    root.mkdir()
    plan = root / "plan.parquet"
    plan.write_bytes(b"plan")
    meta = SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document=design(root, {"rate": [1]}).base_config,
        factors={"rate": [1]},
        seeds=(1,),
        context_hash="c" * 64,
        context_descriptor=design(root, {"rate": [1]}).context_descriptor,
        execution=SimpleNamespace(invocation=("spy",), scratch=tmp_path, workers=1),
        model_root=tmp_path,
        control_states=(),
        calibration_meta_configs=(),
        indicator_requirements=(),
    )
    monkeypatch.setattr(check_service, "load_meta_config", lambda *args, **kwargs: meta)
    monkeypatch.setattr(check_service, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(check_service, "probe_environment", lambda *args: None)
    monkeypatch.setattr(
        check_service,
        "_estimate_state",
        lambda *args: (
            {"estimatedDiskBytes": 100, "estimatedScratchBytes": 20},
            meta,
            (),
            10,
        ),
    )
    monkeypatch.setattr(
        check_service.shutil, "disk_usage", lambda path: SimpleNamespace(free=40)
    )

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    assert payload["status"] == "invalid"
    assert (
        payload["estimatedDiskBytes"],
        payload["requiredDiskBytes"],
        payload["freeDiskBytes"],
        payload["missingDiskBytes"],
        payload["estimatedScratchBytes"],
        payload["freeScratchBytes"],
        payload["missingScratchBytes"],
    ) == (100, 90, 40, 50, 20, 40, 20)
    assert payload["checks"][-1]["error"] == (
        "insufficient disk space: durable missing 50 bytes; scratch missing 20 bytes"
    )


def test_smoke_requires_static_success_and_exactly_two_distinct_seeds(
    monkeypatch,
) -> None:
    from eqasim_sa.adapters import application

    invalid = {"status": "invalid", "checks": []}
    monkeypatch.setattr(check_service, "_static_check", lambda *args: (invalid, None, ()))
    monkeypatch.setattr(
        check_service, "_smoke", lambda *args: (_ for _ in ()).throw(AssertionError())
    )
    assert application.check_from_meta_config(Path("meta.yml"), (), True) is invalid


def test_environment_probe_uses_missing_config_without_loading_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    seen = {}

    def run(command, **options):  # type: ignore[no-untyped-def]
        seen.update(command=command, options=options)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"Config file does not exist: {command[-1]}",
        )

    monkeypatch.setattr("eqasim_sa.adapters.eqasim.simulator.subprocess.run", run)
    probe_environment(("python", "-m", "synpp"), tmp_path)
    assert seen["command"][:3] == ["python", "-m", "synpp"]
    assert Path(seen["command"][-1]).parent == tmp_path
    assert not Path(seen["command"][-1]).exists()
    assert seen["options"]["cwd"] == tmp_path and seen["options"]["shell"] is False


def test_environment_probe_rejects_unexpected_process_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    monkeypatch.setattr(
        "eqasim_sa.adapters.eqasim.simulator.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="wrong"),
    )
    with pytest.raises(RuntimeError, match="environment probe"):
        probe_environment(("synpp",), tmp_path)


def test_environment_probe_reaches_timeout_within_budget(tmp_path: Path) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        probe_environment(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            tmp_path,
            timeout=0.05,
        )
    assert time.monotonic() - started < 1


def test_static_check_never_touches_referenced_data(
    tmp_path: Path, monkeypatch
) -> None:
    from eqasim_sa.adapters import application

    root = tmp_path / "campaign"
    root.mkdir()
    plan = root / "plan.parquet"
    plan.write_bytes(b"plan")
    sentinel = tmp_path / "eleven-gigabytes.csv"
    base = design(root, {"rate": [1]})
    base.base_config["config"]["data_path"] = str(sentinel)
    meta = SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document=base.base_config,
        factors={"rate": [1]},
        seeds=(1,),
        context_hash=base.context_hash,
        context_descriptor=base.context_descriptor,
        execution=SimpleNamespace(invocation=("spy",), scratch=tmp_path, workers=1),
        model_root=tmp_path,
        calibration_meta_configs=(),
        indicator_requirements=(),
        software_identity=read_software_identity(),
    )
    monkeypatch.setattr(
        check_service,
        "load_meta_config",
        lambda path, validate_factors=True, deadline=None: meta,
    )
    monkeypatch.setattr(check_service, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(check_service, "probe_environment", lambda *args: None)
    monkeypatch.setattr(
        check_service,
        "_estimate_state",
        lambda *args: (
            {"estimatedDiskBytes": 4, "estimatedScratchBytes": 0},
            meta,
            (),
            4,
        ),
    )
    original_open, original_stat = Path.open, Path.stat
    original_iterdir, original_read = Path.iterdir, Path.read_bytes

    def forbid(method):  # type: ignore[no-untyped-def]
        return lambda path, *args, **kwargs: (
            (_ for _ in ()).throw(AssertionError(f"data access through {method}"))
            if path == sentinel
            else {
                "open": original_open,
                "stat": original_stat,
                "iterdir": original_iterdir,
                "read_bytes": original_read,
            }[method](path, *args, **kwargs)
        )

    for method in ("open", "stat", "iterdir", "read_bytes"):
        monkeypatch.setattr(Path, method, forbid(method))

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    assert payload["status"] == "valid"


@pytest.mark.parametrize("indicator_case", [None, "missing-column", "duplicate-class"])
def test_smoke_applies_run_overrides_and_computes_every_indicator(
    tmp_path: Path, monkeypatch, indicator_case: str | None
) -> None:
    from eqasim_sa.adapters import application
    from eqasim_sa.adapters.eqasim import execution as eqasim_execution
    from eqasim_sa.core.run import RunSpec

    target = tmp_path / "target"
    target.mkdir()
    config = {
        "working_directory": "/ignored",
        "run": ["synthesis.output", "analysis.synthesis.population"],
        "config": {
            "rate": 1,
            "sampling_rate": 1,
            "random_seed": 1,
            "data_path": "sealed-data",
        },
    }
    spec = RunSpec("demo", config, 1, "c" * 64, {"rate": 1})
    override_data = tmp_path / "override-data"
    override_data.mkdir()
    indicators = (
        ()
        if indicator_case is None
        else (
            TotalVariationIndicator(
                TableArtifact("age", "class", "synthetic", True),
                "reference",
            ),
        )
    )
    meta = SimpleNamespace(
        seeds=(1, 2, 3),
        model_config=tmp_path / "config.yml",
        campaign_root=target,
        model_root=tmp_path,
        execution=SimpleNamespace(
            invocation=("spy",),
            overrides={"data_path": override_data},
            data_path=None,
        ),
        model_document=config,
        model_bytes=b"x",
        structured_factors={},
        indicators=indicators,
        software_identity=read_software_identity(),
        context_hash="c" * 64,
    )
    (tmp_path / "config.yml").write_text("config: {}")
    seen, simulators = [], []

    class Simulator:
        def __init__(self, settings, *args, **kwargs):
            self.settings = settings
            self.workspace = SimpleNamespace(
                root=settings.workspace(0).root,
                session=lambda: nullcontext(self),
            )
            self.interrupted = False
            simulators.append(self)

        def interrupt(self):
            self.interrupted = True

        def simulate(self, resolved, seed, workdir):
            seen.append(
                (
                    seed,
                    resolved["config"]["sampling_rate"],
                    resolved["config"]["data_path"],
                    workdir,
                )
            )
            attempt = workdir / f"attempt-{seed}"
            attempt.mkdir(parents=True)
            names = (
                "persons",
                "households",
                "activities",
                "trips",
                "vehicle_types",
                "vehicles",
                "age",
                "age_purpose",
                "distance",
                "license",
                "nbr_vehicle",
                "travel",
            )
            artifacts = []
            for name in names:
                artifact = attempt / f"{name}.parquet"
                if name == "age" and indicator_case == "duplicate-class":
                    table = pa.table(
                        {
                            "class": ["same", "same"],
                            "synthetic": [1.0, 0.0],
                            "reference": [0.0, 1.0],
                        }
                    )
                else:
                    table = pa.table({"value": [seed]})
                pq.write_table(table, artifact)
                artifacts.append(Artifact(artifact.name, artifact))
            return Manifest(tuple(artifacts), attempt)

    monkeypatch.setattr(eqasim_execution, "EqasimSimulator", Simulator)

    if indicator_case is None:
        check_service._smoke(meta, (spec,))
    else:
        message = "missing declared column" if indicator_case == "missing-column" else "duplicate class labels"
        with pytest.raises(ValueError, match=message):
            check_service._smoke(meta, (spec,))

    assert [(seed, rate, data) for seed, rate, data, _ in seen] == [
        (1, 0.001, str(override_data)),
        (2, 0.001, str(override_data)),
    ]
    assert len(simulators) == 1 and simulators[0].interrupted
    assert all(not workdir.parents[2].exists() for _, _, _, workdir in seen)
    assert not tuple(target.iterdir())


def _static_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch: Path, backend: str = "local"
) -> dict:
    root = tmp_path / "campaign"
    root.mkdir(exist_ok=True)
    meta = SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document={"config": {"rate": 0, "sampling_rate": 1}},
        factors={"rate": [0, 1]},
        seeds=(1, 2),
        method="grid",
        context_hash="c" * 64,
        context_descriptor={"version": "test"},
        execution=SimpleNamespace(
            invocation=("spy",), scratch=scratch, workers=1, backend=backend
        ),
        model_root=tmp_path,
        calibration_meta_configs=(),
        indicator_requirements=(),
        software_identity=read_software_identity(),
    )
    monkeypatch.setattr(check_service, "load_meta_config", lambda *args, **kwargs: meta)
    monkeypatch.setattr(check_service, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(check_service, "unique_config_seed_pairs", lambda design: True)
    monkeypatch.setattr(check_service, "probe_environment", lambda *args: None)
    monkeypatch.setattr(
        check_service,
        "_estimate_state",
        lambda *args: (
            {"estimatedDiskBytes": 0, "estimatedScratchBytes": 0},
            meta,
            (),
            0,
        ),
    )
    return check_service.check_from_meta_config(Path("meta.yml"), (), False)


def _entry(payload: dict, name: str) -> dict:
    return next(item for item in payload["checks"] if item["name"] == name)


def _deep_scratch(tmp_path: Path) -> Path:
    deep = tmp_path / ("x" * 200)
    deep.mkdir()
    return deep


def test_path_length_fails_on_windows_without_long_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "windows_long_paths_enabled", lambda: False)
    payload = _static_payload(tmp_path, monkeypatch, _deep_scratch(tmp_path))
    item = _entry(payload, "path_length")
    assert item["status"] == "failed"
    assert "exceeds the Windows limit of 260 characters" in item["error"]
    assert item["error"].endswith("(LongPathsEnabled) once.")
    assert payload["status"] == "invalid"


def test_path_length_passes_on_windows_with_long_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setattr(platform, "windows_long_paths_enabled", lambda: True)
    payload = _static_payload(tmp_path, monkeypatch, _deep_scratch(tmp_path))
    item = _entry(payload, "path_length")
    assert item["status"] == "passed"
    assert "long paths are enabled" in item["error"]


def test_path_length_warns_on_posix_above_linux_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        check_service, "resolve_root", lambda path, name: Path("/" + "d" * 4100)
    )
    payload = _static_payload(tmp_path, monkeypatch, tmp_path)
    item = _entry(payload, "path_length")
    assert item["status"] == "warning"
    assert "platform limit of 4096" in item["error"]
    assert payload["status"] == "valid"


def test_path_length_warns_on_darwin_above_1024(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        check_service, "resolve_root", lambda path, name: Path("/" + "d" * 1000)
    )
    payload = _static_payload(tmp_path, monkeypatch, tmp_path)
    item = _entry(payload, "path_length")
    assert item["status"] == "warning"
    assert "platform limit of 1024" in item["error"]


def test_path_length_passes_on_posix_within_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    monkeypatch.setattr(sys, "platform", "linux")
    payload = _static_payload(tmp_path, monkeypatch, _deep_scratch(tmp_path))
    item = _entry(payload, "path_length")
    assert item["status"] == "passed"
    assert "within the limit of 4096" in item["error"]


def test_run_rejects_slurm_off_linux_before_importing_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters import application

    meta_file = tmp_path / "meta.yml"
    meta_file.write_text("x", encoding="utf-8")
    meta = SimpleNamespace(execution=SimpleNamespace(backend="slurm"))
    monkeypatch.setattr(application, "load_meta_config", lambda *args, **kw: meta)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="requires Linux"):
        application.run_from_meta_config(meta_file)


def test_slurm_backend_rejected_off_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    payload = _static_payload(tmp_path, monkeypatch, tmp_path, backend="slurm")
    item = _entry(payload, "backend")
    assert item["status"] == "failed"
    assert item["error"] == "the slurm backend requires Linux; run eqasim-sa on the cluster"
    assert payload["status"] == "invalid"


def test_slurm_backend_accepted_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    payload = _static_payload(tmp_path, monkeypatch, tmp_path, backend="slurm")
    assert _entry(payload, "backend")["status"] == "passed"
