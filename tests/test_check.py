import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.core.plan import GridDesign, unique_config_seed_pairs
from eqasim_sa.ports.simulator import Artifact, Manifest


def design(
    root: Path, factors: dict[str, list], seeds: tuple[int, ...] = (1,)
) -> GridDesign:
    return GridDesign(
        {
            "working_directory": "/ignored",
            "run": [],
            "config": {"rate": 1, "sampling_rate": 1},
        },
        factors,
        seeds,
        {"run": ["stage"]},
        "c" * 64,
        {
            "eqasimCommit": "a",
            "dataManifestSha256": "b",
            "lockfileSha256": "c",
            "eqasimSaVersion": "0.1.0",
        },
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
    )
    monkeypatch.setattr(application, "load_meta_config", lambda *args, **kwargs: meta)
    monkeypatch.setattr(application, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(application, "unique_config_seed_pairs", lambda design: False)
    monkeypatch.setattr(application, "probe_cache_coordination", lambda *args: None)
    monkeypatch.setattr(
        application,
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
    monkeypatch.setattr(application, "load_meta_config", lambda *args, **kwargs: meta)
    monkeypatch.setattr(
        application,
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
                "eqasimEnvironment",
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


def test_check_aggregates_environment_duplicate_and_disk_errors(
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
        application,
        "load_meta_config",
        lambda path, validate_factors=True, deadline=None: meta,
    )
    monkeypatch.setattr(application, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(application, "unique_config_seed_pairs", lambda value: False)
    monkeypatch.setattr(
        application,
        "_estimate_state",
        lambda *args: (_ for _ in ()).throw(ValueError("no calibration")),
    )
    monkeypatch.setattr(
        application,
        "probe_cache_coordination",
        lambda *args: (_ for _ in ()).throw(OSError("unreachable")),
    )
    plan.write_bytes(b"plan")

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    assert payload["status"] == "invalid"
    errors = {item["name"]: item["error"] for item in payload["checks"]}
    assert "duplicate" in errors["uniqueConfigSeedPairs"]
    assert "unreachable" in errors["eqasimEnvironment"]
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
    monkeypatch.setattr(application, "load_meta_config", lambda *args, **kwargs: meta)

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
    monkeypatch.setattr(application, "load_meta_config", lambda *args, **kwargs: meta)
    monkeypatch.setattr(application, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(application, "probe_cache_coordination", lambda *args: None)
    monkeypatch.setattr(
        application,
        "_estimate_state",
        lambda *args: ({"estimatedDiskBytes": 100}, meta, (), 10),
    )
    monkeypatch.setattr(
        application.shutil, "disk_usage", lambda path: SimpleNamespace(free=40)
    )

    payload = application.check_from_meta_config(Path("meta.yml"), (), False)

    assert payload["status"] == "invalid"
    assert (
        payload["estimatedDiskBytes"],
        payload["requiredDiskBytes"],
        payload["freeDiskBytes"],
        payload["missingDiskBytes"],
    ) == (100, 90, 40, 50)
    assert payload["checks"][-1]["error"] == "insufficient disk space: missing 50 bytes"


def test_smoke_requires_static_success_and_exactly_two_distinct_seeds(
    monkeypatch,
) -> None:
    from eqasim_sa.adapters import application

    invalid = {"status": "invalid", "checks": []}
    monkeypatch.setattr(application, "_static_check", lambda *args: (invalid, None, ()))
    monkeypatch.setattr(
        application, "_smoke", lambda *args: (_ for _ in ()).throw(AssertionError())
    )
    assert application.check_from_meta_config(Path("meta.yml"), (), True) is invalid


def test_environment_probe_uses_exact_argv_cwd_and_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_cache_coordination

    seen = {}

    def run(command, **options):  # type: ignore[no-untyped-def]
        seen.update(command=command, options=options)
        return SimpleNamespace(returncode=0, stdout="synpp-cache-coordination-v1\n")

    monkeypatch.setattr("eqasim_sa.adapters.eqasim.simulator.subprocess.run", run)
    probe_cache_coordination(("python", "spy.py"), tmp_path)
    assert seen["command"] == ["python", "spy.py", "--cache-coordination-capability"]
    assert seen["options"]["cwd"] == tmp_path and seen["options"]["shell"] is False
    assert seen["options"]["timeout"] < 60


@pytest.mark.parametrize(
    "invalid_outputs",
    [
        ("", "synpp-cache-coordination-v1"),
        (" synpp-cache-coordination-v1\n", "synpp-cache-coordination-v1 \n"),
        ("synpp-cache-coordination-v1\n\n",),
        ("synpp-cache-coordination-v1 extra\n", "banner\nsynpp-cache-coordination-v1\n"),
        ("synpp-cache-coordination-v2\n",),
    ],
)
def test_environment_probe_rejects_any_non_exact_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_outputs: tuple[str, ...],
) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_cache_coordination

    for stdout in invalid_outputs:
        monkeypatch.setattr(
            "eqasim_sa.adapters.eqasim.simulator.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout=stdout, stderr=""
            ),
        )
        with pytest.raises(RuntimeError, match="capability mismatch"):
            probe_cache_coordination(("synpp",), tmp_path)


def test_environment_probe_reaches_timeout_within_budget(tmp_path: Path) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_cache_coordination

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        probe_cache_coordination(
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
        context_hash="c" * 64,
        context_descriptor=base.context_descriptor,
        execution=SimpleNamespace(invocation=("spy",), scratch=tmp_path, workers=1),
        model_root=tmp_path,
        calibration_meta_configs=(),
        indicator_requirements=(),
    )
    monkeypatch.setattr(
        application,
        "load_meta_config",
        lambda path, validate_factors=True, deadline=None: meta,
    )
    monkeypatch.setattr(application, "load_run_specs", lambda *args: ())
    monkeypatch.setattr(application, "probe_cache_coordination", lambda *args: None)
    monkeypatch.setattr(
        application,
        "_estimate_state",
        lambda *args: ({"estimatedDiskBytes": 4}, meta, (), 4),
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


def test_smoke_executes_exactly_two_runs_and_removes_owned_paths(
    tmp_path: Path, monkeypatch
) -> None:
    from eqasim_sa.adapters import application
    from eqasim_sa.core.run import RunSpec

    target = tmp_path / "target"
    target.mkdir()
    config = {
        "working_directory": "/ignored",
        "run": ["synthesis.output", "analysis.synthesis.population"],
        "config": {"rate": 1, "sampling_rate": 1, "random_seed": 1},
    }
    spec = RunSpec("demo", config, 1, "c" * 64, {"rate": 1})
    meta = SimpleNamespace(
        seeds=(1, 2, 3),
        model_config=tmp_path / "config.yml",
        campaign_root=target,
        model_root=tmp_path,
        execution=SimpleNamespace(invocation=("spy",)),
        model_document=config,
        model_bytes=b"x",
    )
    (tmp_path / "config.yml").write_text("config: {}")
    seen, simulators = [], []

    class Simulator:
        def __init__(self, settings, *args):
            self.settings = settings
            self.interrupted = False
            simulators.append(self)

        def interrupt(self):
            self.interrupted = True

        def simulate(self, resolved, seed, workdir):
            seen.append((seed, resolved["config"]["sampling_rate"], workdir))
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
                pq.write_table(pa.table({"value": [seed]}), artifact)
                artifacts.append(Artifact(artifact.name, artifact))
            return Manifest(tuple(artifacts), attempt)

    monkeypatch.setattr(application, "EqasimSimulator", Simulator)

    application._smoke(meta, (spec,))

    assert [(seed, rate) for seed, rate, _ in seen] == [(1, 0.001), (2, 0.001)]
    assert len(simulators) == 1 and simulators[0].interrupted
    assert all(not workdir.parents[2].exists() for _, _, workdir in seen)
    assert not tuple(target.iterdir())
