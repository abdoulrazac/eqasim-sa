import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.adapters import estimate as estimate_service
from eqasim_sa.core.ledger import RunRecord, write_run
from eqasim_sa.core.plan import (
    GridDesign,
    MorrisDesign,
    SobolDesign,
    build_plan,
    estimate_plan,
    load_run_specs,
)
from eqasim_sa.core.run import compute_run_id
from eqasim_sa.ports.simulator import IndicatorRequirement
from tests.provenance_helpers import context_hash


def test_estimate_plan_uses_unique_runs_and_observed_arithmetic_means() -> None:
    payload = estimate_plan(
        design_positions=3,
        run_ids=("run-a", "run-a", "run-b"),
        plan_bytes=100,
        observations=((2.0, 10, 40), (4.0, 20, 60)),
        workers=3,
    )

    assert payload == {
        "status": "estimated",
        "estimateVersion": 2,
        "method": "grid",
        "formula": None,
        "formulaInputs": None,
        "designPositions": 3,
        "runs": 2,
        "calibrationRuns": 2,
        "meanDurationSeconds": 3.0,
        "meanDurableBytesPerRun": 15.0,
        "estimatedComputeHours": 2 * 3.0 / 3600,
        "estimatedDiskBytes": 130,
        "workers": 3,
        "observedScratchBytesPerWorker": 60,
        "estimatedScratchBytes": 180,
        "error": None,
    }


def test_estimate_plan_rounds_fractional_disk_bytes_up() -> None:
    payload = estimate_plan(2, ("a", "b"), 10, ((1.0, 1, 3), (1.0, 2, 4)), 2)

    assert payload["meanDurableBytesPerRun"] == 1.5
    assert payload["estimatedDiskBytes"] == 13
    assert type(payload["estimatedDiskBytes"]) is int


@pytest.mark.parametrize(
    "observation",
    [(-1.0, 1, 1), (math.nan, 1, 1), (math.inf, 1, 1), (1.0, -1, 1), (1.0, 1, -1)],
)
def test_estimate_plan_rejects_invalid_observations(
    observation: tuple[float, int, int],
) -> None:
    with pytest.raises(ValueError, match="observation"):
        estimate_plan(1, ("run",), 1, (observation,), 1)


def test_estimate_plan_requires_calibration() -> None:
    with pytest.raises(ValueError, match="calibration meta-config"):
        estimate_plan(1, ("run",), 1, (), 1)


def test_estimate_plan_exposes_and_validates_sobol_formula() -> None:
    payload = estimate_plan(
        12, ("a", "a", "b"), 7, ((1.0, 3, 5),), 1, "sobol", {"D": 2, "N": 2}
    )

    assert payload["formula"] == "N(2D+2)"
    assert payload["formulaInputs"] == {"D": 2, "N": 2}
    assert payload["runs"] == 2
    with pytest.raises(ValueError, match="designPositions"):
        estimate_plan(11, ("a",), 7, ((1.0, 3, 5),), 1, "sobol", {"D": 2, "N": 2})


def test_estimate_plan_exposes_morris_formula_and_counts_unique_runs() -> None:
    payload = estimate_plan(
        24, ("a", "a", "b"), 7, ((1.0, 3, 5),), 1, "morris", {"D": 2, "T": 4, "R": 2}
    )
    assert payload["formula"] == "T(D+1)R"
    assert payload["formulaInputs"] == {"D": 2, "T": 4, "R": 2}
    assert payload["designPositions"] == 24 and payload["runs"] == 2
    with pytest.raises(ValueError, match="designPositions"):
        estimate_plan(23, ("a",), 7, ((1.0, 3, 5),), 1, "morris", {"D": 2, "T": 4, "R": 2})


def test_application_estimate_passes_morris_formula_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application

    target = campaign(tmp_path / "target", morris=True)
    monkeypatch.setattr(estimate_service, "load_meta_config", lambda path: target)
    seen = {}
    original = estimate_service.estimate_plan

    def capture(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(estimate_service, "estimate_plan", capture)
    payload = application.estimate_from_meta_config(Path("target.yml"), ())

    assert payload["method"] == "morris"
    assert payload["formula"] == "T(D+1)R"
    assert payload["formulaInputs"] == {"D": 1, "T": 2, "R": 1}


def test_application_estimate_passes_sobol_formula_inputs_and_unique_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters import application

    root = tmp_path / "target"
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    sealed_context_hash = context_hash(descriptor)
    design = SobolDesign(
        {"config": {"rate": 0}},
        {"rate": [0, 1]},
        (7, 8),
        {"run": ["synthesis.output", "analysis.synthesis.population"]},
        sealed_context_hash,
        descriptor,
        root,
        base_sample_size=8,
        design_seed=7,
    )
    built = build_plan(design)
    spec = load_run_specs(root / "plan.parquet", design, "demo")[0]
    run_id = compute_run_id(spec.resolved_config, spec.seed)
    marker = write_run(
        root,
        RunRecord(
            run_id,
            "demo",
            spec.seed,
            sealed_context_hash,
            "node",
            2.0,
            spec.parameters,
        ),
    )
    archive = root / "archives" / run_id
    archive.mkdir(parents=True)
    pq.write_table(pa.table({"value": [1]}), archive / "output.parquet")
    target = SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document=design.base_config,
        factors=design.factors,
        seeds=design.seeds,
        context_hash=design.context_hash,
        context_descriptor=descriptor,
        marker=marker,
        execution=SimpleNamespace(workers=3),
        calibration_meta_configs=(),
        indicator_requirements=(IndicatorRequirement("value", "age"),),
        method="sobol",
        trajectories=None,
        design_seed=7,
        base_sample_size=8,
    )
    monkeypatch.setattr(estimate_service, "load_meta_config", lambda path: target)

    payload = application.estimate_from_meta_config(Path("target.yml"), ())

    assert built.design_positions == 48
    assert payload["formula"] == "N(2D+2)"
    assert payload["formulaInputs"] == {"D": 2, "N": 8}
    assert payload["designPositions"] == 48
    assert payload["runs"] == built.unique_runs


def campaign(
    root: Path, duration: float = 2.5, morris: bool = False
) -> SimpleNamespace:
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    sealed_context_hash = context_hash(descriptor)
    common = (
        {"config": {"rate": 0}},
        {"rate": [0, 1] if morris else [1, 1]},
        (7,),
        {"run": ["synthesis.output", "analysis.synthesis.population"]},
        sealed_context_hash,
        descriptor,
        root,
    )
    design = (
        MorrisDesign(*common, trajectories=2, design_seed=7)
        if morris
        else GridDesign(*common)
    )
    build_plan(design)
    spec = load_run_specs(root / "plan.parquet", design, "demo")[0]
    run_id = compute_run_id(spec.resolved_config, spec.seed)
    marker = write_run(
        root,
        RunRecord(
            run_id,
            "demo",
            7,
            sealed_context_hash,
            "node",
            duration,
            spec.parameters,
            100,
        ),
    )
    archive = root / "archives" / run_id
    archive.mkdir(parents=True)
    pq.write_table(pa.table({"value": [1]}), archive / "output.parquet")
    return SimpleNamespace(
        campaign="demo",
        campaign_root=root,
        model_document=design.base_config,
        factors=design.factors,
        seeds=design.seeds,
        context_hash=design.context_hash,
        context_descriptor=descriptor,
        marker=marker,
        execution=SimpleNamespace(workers=3),
        calibration_meta_configs=(),
        indicator_requirements=(IndicatorRequirement("value", "age"),),
        method=design.method,
        trajectories=getattr(design, "trajectories", None),
        design_seed=getattr(design, "design_seed", None),
    )


def test_application_estimate_collects_complete_runs_once_and_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.application import estimate_from_meta_config

    target, calibration = (
        campaign(tmp_path / "target"),
        campaign(tmp_path / "calibration", 7.5),
    )
    metas = {
        Path("target.yml"): target,
        Path("calibration.yml"): calibration,
        Path("same.yml"): calibration,
    }
    monkeypatch.setattr(
        "eqasim_sa.adapters.estimate.load_meta_config", metas.__getitem__
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for root in (target.campaign_root, calibration.campaign_root)
        for path in root.rglob("*")
        if path.is_file()
    }

    payload = estimate_from_meta_config(
        Path("target.yml"), (Path("calibration.yml"), Path("same.yml"))
    )

    expected_bytes = (
        sum(
            path.stat().st_size
            for path in (
                target.marker,
                next((target.campaign_root / "archives").rglob("*.parquet")),
                calibration.marker,
                next((calibration.campaign_root / "archives").rglob("*.parquet")),
            )
        )
        / 2
    )
    assert payload["designPositions"] == 2 and payload["runs"] == 1
    assert payload["calibrationRuns"] == 2 and payload["meanDurationSeconds"] == 5.0
    assert payload["meanDurableBytesPerRun"] == expected_bytes
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before
    } == before


@pytest.mark.parametrize(
    "damage", ["missing", "empty", "directory", "foreign", "symlink", "truncated"]
)
def test_application_estimate_rejects_unsafe_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    from eqasim_sa.adapters.application import estimate_from_meta_config

    meta = campaign(tmp_path / "campaign")
    archive = next((meta.campaign_root / "archives").iterdir())
    parquet = archive / "output.parquet"
    if damage == "missing":
        __import__("shutil").rmtree(archive)
    elif damage == "empty":
        parquet.unlink()
    elif damage == "directory":
        (archive / "nested").mkdir()
    elif damage == "foreign":
        (archive / "notes.txt").write_text("x")
    elif damage == "truncated":
        parquet.write_bytes(b"PAR1")
    else:
        parquet.unlink()
        parquet.symlink_to(meta.marker)
    monkeypatch.setattr(
        "eqasim_sa.adapters.estimate.load_meta_config", lambda path: meta
    )
    with pytest.raises(ValueError, match="archive"):
        estimate_from_meta_config(Path("meta.yml"), ())


def test_application_estimate_rejects_archive_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.application import estimate_from_meta_config

    meta = campaign(tmp_path / "campaign")
    monkeypatch.setattr(
        "eqasim_sa.adapters.estimate.load_meta_config", lambda path: meta
    )
    original = pq.ParquetFile
    mutated = False

    def mutate_after_open(path: Path):  # type: ignore[no-untyped-def]
        nonlocal mutated
        result = original(path)
        if (
            isinstance(path, (str, Path))
            and Path(path).parent.parent.name == "archives"
            and not mutated
        ):
            mutated = True
            pq.write_table(
                pa.table({"value": [2]}), Path(path).with_name("added.parquet")
            )
        return result

    monkeypatch.setattr(
        "eqasim_sa.core.campaign_snapshot.pq.ParquetFile", mutate_after_open
    )
    with pytest.raises(RuntimeError, match="mutated"):
        estimate_from_meta_config(Path("meta.yml"), ())


def test_application_estimate_rejects_target_plan_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.adapters.application import estimate_from_meta_config

    meta = campaign(tmp_path / "campaign")
    monkeypatch.setattr(
        "eqasim_sa.adapters.estimate.load_meta_config", lambda path: meta
    )
    original = pq.ParquetFile
    plan_reads = 0

    def replace_after_validation(path: Path):  # type: ignore[no-untyped-def]
        nonlocal plan_reads
        result = original(path)
        if isinstance(path, (str, Path)) and Path(path).name == "plan.parquet":
            plan_reads += 1
            if plan_reads == 1:
                replacement = Path(path).with_suffix(".replacement")
                replacement.write_bytes(Path(path).read_bytes())
                replacement.replace(path)
        return result

    monkeypatch.setattr(
        "eqasim_sa.core.campaign_snapshot.pq.ParquetFile", replace_after_validation
    )
    with pytest.raises(RuntimeError, match="plan mutated"):
        estimate_from_meta_config(Path("meta.yml"), ())


def test_estimate_cli_serializes_exact_success_and_failure_schema(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from eqasim_sa.cli import dispatch

    expected = estimate_plan(1, ("run",), 1, ((1.0, 2, 3),), 1)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eqasim-sa",
            "estimate",
            "--meta-config",
            "target.yml",
            "--calibration-meta-config",
            "calibration.yml",
        ],
    )
    assert dispatch(None, None, estimate_factory=lambda target, sources: expected) == 0
    first = capsys.readouterr().out
    assert (
        json.loads(first) == expected
        and first == json.dumps(expected, sort_keys=True) + "\n"
    )

    monkeypatch.setattr(
        sys, "argv", ["eqasim-sa", "estimate", "--meta-config", "target.yml"]
    )
    assert (
        dispatch(
            None,
            None,
            estimate_factory=lambda target, sources: (_ for _ in ()).throw(Exception()),
        )
        == 1
    )
    failure = json.loads(capsys.readouterr().out)
    assert set(failure) == set(expected)
    assert failure == {
        "status": "failed",
        "estimateVersion": 2,
        "method": None,
        "formula": None,
        "formulaInputs": None,
        "designPositions": 0,
        "runs": 0,
        "calibrationRuns": 0,
        "meanDurationSeconds": None,
        "meanDurableBytesPerRun": None,
        "estimatedComputeHours": None,
        "estimatedDiskBytes": 0,
        "workers": None,
        "observedScratchBytesPerWorker": None,
        "estimatedScratchBytes": 0,
        "error": "Exception",
    }
