from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import eqasim_sa.core.ledger as ledger_module
from eqasim_sa.core.campaign_snapshot import CampaignSnapshot
from eqasim_sa.core.ledger import RunRecord, archive_transaction
from eqasim_sa.core.run import RunSpec, compute_run_id


def spec(value: int) -> RunSpec:
    config = {"config": {"random_seed": 42, "x": value}}
    return RunSpec("demo", config, 42, "c" * 64, {"x": value})


def plan(root: Path, specs: tuple[RunSpec, ...]) -> Path:
    rows = []
    for index, current in enumerate(specs):
        rows.append(
            {
                "designId": f"design-{index}",
                "method": "grid",
                "role": "point",
                "runId": compute_run_id(current.resolved_config, current.seed),
                "seed": current.seed,
                "contextHash": current.expected_context_hash,
                "x": current.parameters["x"],
            }
        )
    destination = root / "plan.parquet"
    root.mkdir()
    pq.write_table(pa.Table.from_pylist(rows), destination, compression="zstd")
    return destination


def complete(root: Path, current: RunSpec, value: int) -> None:
    run_id = compute_run_id(current.resolved_config, current.seed)
    source = root / f"source-{value}.parquet"
    pq.write_table(pa.table({"value": [value]}), source, compression="zstd")
    record = RunRecord(
        run_id,
        current.campaign,
        current.seed,
        current.expected_context_hash,
        "node",
        2.5,
        current.parameters,
        128,
    )
    with archive_transaction(root, run_id, (("output.parquet", source),)) as publication:
        publication.commit(record)


def test_snapshot_captures_plan_runs_archives_and_measurements(tmp_path: Path) -> None:
    specs = (spec(1), spec(2))
    plan_path = plan(tmp_path / "campaign", specs)
    complete(plan_path.parent, specs[0], 10)

    snapshot = CampaignSnapshot.capture(plan_path.parent, plan_path, "demo")
    materialized = snapshot.materialize(tmp_path / "capture")

    run_id = compute_run_id(specs[0].resolved_config, specs[0].seed)
    assert snapshot.expected_ids == {
        compute_run_id(current.resolved_config, current.seed) for current in specs
    }
    assert snapshot.completed_ids == {run_id}
    assert snapshot.measurements == ((2.5, snapshot.runs[run_id].durable_bytes, 128),)
    assert pq.read_table(materialized.artifacts[run_id].path("output")).to_pylist() == [
        {"value": 10}
    ]
    snapshot.verify()


def test_snapshot_ignores_run_completed_after_its_cut(tmp_path: Path) -> None:
    specs = (spec(1), spec(2))
    plan_path = plan(tmp_path / "campaign", specs)
    complete(plan_path.parent, specs[0], 10)
    snapshot = CampaignSnapshot.capture(plan_path.parent, plan_path, "demo")

    complete(plan_path.parent, specs[1], 20)

    assert snapshot.completed_ids == {
        compute_run_id(specs[0].resolved_config, specs[0].seed)
    }
    materialized = snapshot.materialize(tmp_path / "capture")
    assert set(materialized.artifacts) == set(snapshot.completed_ids)


def test_snapshot_refuses_selected_archive_mutation(tmp_path: Path) -> None:
    specs = (spec(1),)
    plan_path = plan(tmp_path / "campaign", specs)
    complete(plan_path.parent, specs[0], 10)
    snapshot = CampaignSnapshot.capture(plan_path.parent, plan_path, "demo")
    run_id = next(iter(snapshot.completed_ids))

    snapshot.runs[run_id].artifacts[0].path.write_bytes(b"mutated")

    with pytest.raises(RuntimeError, match="selected analysis source"):
        snapshot.verify()


def test_completed_specs_uses_the_same_stable_cut(tmp_path: Path) -> None:
    specs = (spec(1), spec(2))
    root = tmp_path / "campaign"
    plan(root, specs)
    complete(root, specs[1], 20)

    assert CampaignSnapshot.completed_specs(root, specs) == {
        compute_run_id(specs[1].resolved_config, specs[1].seed)
    }


def test_snapshot_hashes_each_selected_marker_and_artifact_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = (spec(1),)
    root = tmp_path / "campaign"
    plan_path = plan(root, specs)
    complete(root, specs[0], 10)
    real_hash = ledger_module.descriptor_sha256
    calls = 0

    def counted_hash(descriptor: int, interrupted=None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real_hash(descriptor, interrupted)

    monkeypatch.setattr(ledger_module, "descriptor_sha256", counted_hash)

    snapshot = CampaignSnapshot.capture(root, plan_path, "demo")

    assert len(snapshot.runs[next(iter(snapshot.completed_ids))].artifacts) == 1
    assert calls == 2
