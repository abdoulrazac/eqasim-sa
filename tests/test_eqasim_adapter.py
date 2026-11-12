import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

import eqasim_sa.adapters.eqasim.artifacts as artifacts_module
from eqasim_sa.adapters.eqasim.artifacts import normalize_outputs
from eqasim_sa.adapters.eqasim.config import EqasimSettings, load_config
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator
from eqasim_sa.ports.simulator import IndicatorRequirement, SimulationError
from tests.conftest import make_symlink


def write_yaml(path: Path, *, prefix: str = "nantes_") -> None:
    path.write_text(
        "working_directory: /ignored\n"
        "run: [old.stage]\n"
        "config:\n"
        "  sampling_rate: 0.001\n"
        "  random_seed: 12345678\n"
        f"  output_prefix: {prefix!r}\n"
        "  output_path: /historical/output\n"
        "  processes: 1\n"
        "  departments: [44]\n",
        encoding="utf-8",
    )


def settings(tmp_path: Path) -> EqasimSettings:
    config = tmp_path / "config.yml"
    write_yaml(config)
    (tmp_path / "scratch").mkdir()
    return EqasimSettings(
        config_path=config,
        eqasim_root=tmp_path,
        invocation=(sys.executable, str(Path(__file__).with_name("eqasim_spy.py"))),
        campaign="nantes.v1",
        scratch=tmp_path / "scratch",
        context_hash="c" * 64,
    )


def simulator(current: EqasimSettings, seed: int = 42) -> EqasimSimulator:
    return EqasimSimulator(current, load_config(current.config_path, seed))


def test_workdir_is_derived_only_from_scratch_and_campaign(tmp_path: Path) -> None:
    first = settings(tmp_path)
    same_campaign = EqasimSettings(
        first.config_path,
        first.eqasim_root,
        first.invocation,
        first.campaign,
        first.scratch,
        first.context_hash,
    )
    other_campaign = EqasimSettings(
        first.config_path,
        first.eqasim_root,
        first.invocation,
        "nantes-calibration",
        first.scratch,
        first.context_hash,
    )

    assert first.workdir == (
        tmp_path / "scratch" / "esa" / "nantes.v1" / "w0"
    )
    assert same_campaign.workdir == first.workdir
    assert other_campaign.workdir == (
        tmp_path / "scratch" / "esa" / "nantes-calibration" / "w0"
    )
    assert other_campaign.workdir != first.workdir

    node_roots = (tmp_path / "node-a", tmp_path / "node-b")
    for root in node_roots:
        root.mkdir()
    per_node = tuple(
        EqasimSettings(
            first.config_path,
            first.eqasim_root,
            first.invocation,
            first.campaign,
            root,
            first.context_hash,
        ).workdir
        for root in node_roots
    )
    assert per_node[0] != per_node[1]
    assert tuple(
        path.relative_to(root) for path, root in zip(per_node, node_roots)
    ) == (
        Path("esa/nantes.v1/w0"),
        Path("esa/nantes.v1/w0"),
    )


def test_worker_cache_is_namespaced_by_the_full_context_hash(tmp_path: Path) -> None:
    current = settings(tmp_path)
    first_hash, second_hash = "a" * 64, "b" * 64
    first = EqasimSettings(
        current.config_path,
        current.eqasim_root,
        current.invocation,
        current.campaign,
        current.scratch,
        first_hash,
    ).workspace(3)
    second = EqasimSettings(
        current.config_path,
        current.eqasim_root,
        current.invocation,
        current.campaign,
        current.scratch,
        second_hash,
    ).workspace(3)

    assert first.root == second.root
    assert first.cache == first.root / ("c" + first_hash[:12])
    assert second.cache == second.root / ("c" + second_hash[:12])
    assert first.cache != second.cache


def test_context_cache_state_isolation_prevents_cross_context_reuse(
    tmp_path: Path,
) -> None:
    current = settings(tmp_path)

    def adapter(context_hash: str) -> EqasimSimulator:
        scoped = EqasimSettings(
            current.config_path,
            current.eqasim_root,
            current.invocation,
            current.campaign,
            current.scratch,
            context_hash,
        )
        return EqasimSimulator(scoped, load_config(scoped.config_path, 42))

    first, second = adapter("a" * 64), adapter("b" * 64)
    with first.workspace.session():
        first.workspace.prepare_cache()
        sentinel = first.workspace.cache / "stale"
        sentinel.write_text("first context")
        first.workspace.mark_active()
    with second.workspace.session():
        second.workspace.prepare_cache()
        second.workspace.mark_clean()
    assert sentinel.is_file()
    with first.workspace.session():
        first.workspace.prepare_cache()
    assert not sentinel.exists()


def test_simulator_recreates_deleted_workdir_and_preserves_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    resolved = load_config(current.config_path, 42)

    simulator(current).simulate(resolved, 42, current.workdir)
    shutil.rmtree(current.workdir)
    assert not current.workdir.exists()

    manifest = simulator(current).simulate(resolved, 42, current.workdir)
    assert current.workdir.is_dir()
    assert manifest.attempt_directory.parent == current.workdir
    with pytest.raises(ValueError, match="workdir must be"):
        simulator(current).simulate(resolved, 42, tmp_path / "outside")


def test_loads_yaml_read_only_and_resolves_seed_and_stages(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    write_yaml(config)
    before = hashlib.sha256(config.read_bytes()).hexdigest()
    resolved = load_config(config, 42)
    assert resolved["run"] == ["synthesis.output", "analysis.synthesis.population"]
    assert resolved["config"]["random_seed"] == 42
    assert resolved["config"]["departments"] == [44]
    assert hashlib.sha256(config.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "prefix",
    ["../escape", "a/b", "a\\b", "bad..name", "_hidden", ".hidden", "-dash"],
)
def test_rejects_unsafe_output_prefix(tmp_path: Path, prefix: str) -> None:
    current = settings(tmp_path)
    write_yaml(current.config_path, prefix=prefix)
    adapter = simulator(current)
    with pytest.raises(ValueError, match="output_prefix"):
        adapter.simulate(load_config(current.config_path, 42), 42, current.workdir)


def test_real_spy_subprocess_normalizes_exact_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    before = current.config_path.read_bytes()
    resolved = load_config(current.config_path, 42)
    adapter = EqasimSimulator(current, resolved, before)
    current.config_path.write_text("config:\n  replaced: true\n", encoding="utf-8")
    manifest = adapter.simulate(resolved, 42, current.workdir)
    assert {artifact.name for artifact in manifest.artifacts} == {
        "persons.parquet",
        "households.parquet",
        "activities.parquet",
        "trips.parquet",
        "vehicle_types.parquet",
        "vehicles.parquet",
        "age.parquet",
        "age_purpose.parquet",
        "distance.parquet",
        "license.parquet",
        "nbr_vehicle.parquet",
        "travel.parquet",
    }
    observed = json.loads(record.read_text())
    assert observed["pid"] != os.getpid()
    assert observed["cwd"] == str(tmp_path)
    attempt = manifest.attempt_directory
    assert observed["argv"] == [
        str(attempt / "config.yml"),
        "--working-directory",
        str(current.workspace(0).cache),
        "--run",
        "synthesis.output",
        "--run",
        "analysis.synthesis.population",
        "--random_seed",
        "42",
        "--output_path",
        str(attempt / "output"),
        "--output_prefix",
        "nantes_",
    ]
    assert current.config_path.read_bytes() != before
    assert (attempt / "config.yml").read_bytes() == before
    age = pq.read_table(
        next(a.path for a in manifest.artifacts if a.name == "age.parquet")
    )
    assert age.column_names == [
        "Age class",
        "HTS adjust with INSEE population weights",
        "Proportion_EQASIM",
    ]
    purpose = pq.read_table(
        next(a.path for a in manifest.artifacts if a.name == "age_purpose.parquet")
    )
    assert purpose.column_names == ["age_class", "Home<>Work"]
    assert age["Age class"].to_pylist() == ["65+", "(99.0, inf]"]
    assert age["HTS adjust with INSEE population weights"].null_count == 1
    assert {path.name for path in (attempt / "normalized").iterdir()} == {
        artifact.name for artifact in manifest.artifacts
    }


def test_execution_guard_wraps_the_complete_simulation_and_supplies_child_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))

    class Seal:
        def __init__(self) -> None:
            self.calls = 0
            self.environment = dict(os.environ)
            self.evidence = b"sealed evidence"

        def guard(self) -> None:
            self.calls += 1

    seal = Seal()
    adapter = EqasimSimulator(
        current,
        load_config(current.config_path, 42),
        execution_seal=seal,  # type: ignore[arg-type]
    )
    manifest = adapter.simulate(
        load_config(current.config_path, 42), 42, current.workdir
    )

    assert seal.calls == 2
    assert manifest.execution_evidence == b"sealed evidence"
    assert record.is_file()


def test_post_simulation_guard_failure_leaves_cache_state_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))

    class Seal:
        environment = dict(os.environ)
        evidence = b"unused"
        calls = 0

        def guard(self) -> None:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("post-simulation drift")

    adapter = EqasimSimulator(
        current,
        load_config(current.config_path, 42),
        execution_seal=Seal(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="post-simulation drift"):
        adapter.simulate(load_config(current.config_path, 42), 42, current.workdir)

    assert adapter.workspace.status() == "active"


def test_post_simulation_cache_validation_precedes_measurement_and_clean_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = settings(tmp_path)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    adapter = simulator(current)
    real_run = adapter._run_owned

    def corrupt_cache(*arguments, **options):  # type: ignore[no-untyped-def]
        completed = real_run(*arguments, **options)
        target = adapter.workspace.cache / "owned"
        target.write_text("cache\n")
        os.link(target, adapter.workspace.cache / "hardlink")
        return completed

    monkeypatch.setattr(adapter, "_run_owned", corrupt_cache)

    with pytest.raises(ValueError, match="hard links"):
        adapter.simulate(load_config(current.config_path, 42), 42, current.workdir)

    assert adapter.workspace.status() == "active"


def test_executes_config_run_and_normalizes_only_requested_stage_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    resolved = load_config(current.config_path, 42)
    resolved["run"] = ["synthesis.output"]
    manifest = simulator(current).simulate(resolved, 42, current.workdir)
    assert {artifact.name for artifact in manifest.artifacts} == {
        "persons.parquet",
        "households.parquet",
        "activities.parquet",
        "trips.parquet",
        "vehicle_types.parquet",
        "vehicles.parquet",
    }
    arguments = json.loads(record.read_text())["argv"]
    assert arguments[arguments.index("--run") + 1] == "synthesis.output"
    assert "analysis.synthesis.population" not in arguments


@pytest.mark.parametrize("stages", [[], ["unknown.stage"]])
def test_rejects_empty_or_unknown_run_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stages: list[str],
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    resolved = load_config(current.config_path, 42)
    resolved["run"] = stages
    with pytest.raises(ValueError, match="run|stage"):
        simulator(current).simulate(resolved, 42, current.workdir)
    assert not record.exists()


def test_requirements_deduplicate_stages_in_request_order() -> None:
    adapter = object.__new__(EqasimSimulator)
    assert adapter.requirements(
        (
            IndicatorRequirement("distance", "distance"),
            IndicatorRequirement("persons", "persons"),
            IndicatorRequirement("age", "age"),
        )
    ) == {"run": ["synthesis.output", "analysis.synthesis.population"]}


def test_failed_process_leaves_active_marker_and_next_run_purges_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    monkeypatch.setenv("EQASIM_SPY_FAIL", "7")
    adapter = simulator(current)
    with pytest.raises(SimulationError, match="code 7"):
        adapter.simulate(
            load_config(current.config_path, 42), 42, current.workdir
        )
    workspace = current.workspace(0)
    assert workspace.status() == "active"
    sentinel = workspace.cache / "stale"
    sentinel.write_text("purge me")
    monkeypatch.delenv("EQASIM_SPY_FAIL")
    adapter.simulate(load_config(current.config_path, 42), 42, current.workdir)
    assert not sentinel.exists()
    assert workspace.status() == "clean"


def test_malformed_marker_purges_cache_before_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = settings(tmp_path)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    workspace = current.workspace(0)
    workspace.cache.mkdir(parents=True)
    sentinel = workspace.cache / "stale"
    sentinel.write_text("stale")
    state = workspace.root / "state.json"
    state.write_text("not-json")
    simulator(current).simulate(
        load_config(current.config_path, 42), 42, workspace.root
    )
    assert not sentinel.exists()


def test_slot_lease_wait_is_interruptible(tmp_path: Path) -> None:
    import threading
    import time

    current = settings(tmp_path)
    first, second = simulator(current), simulator(current)
    acquired = threading.Event()
    stopped = threading.Event()
    with first.workspace.session():
        def wait() -> None:
            try:
                with second.workspace.session():
                    acquired.set()
            except KeyboardInterrupt:
                stopped.set()

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.1)
        assert not acquired.is_set()
        second.interrupt()
        thread.join(timeout=2)
    assert stopped.is_set() and not acquired.is_set()


def test_slot_lease_rejects_hard_link(tmp_path: Path) -> None:
    current = settings(tmp_path)
    adapter = simulator(current)
    adapter.workspace.root.mkdir(parents=True)
    source = tmp_path / "foreign-lock"
    source.write_text("foreign")
    os.link(source, adapter.workspace.root / "lease.lock")

    with pytest.raises(ValueError, match="private owned regular file"):
        with adapter.workspace.session():
            pass


def test_missing_output_and_process_failure_raise_logged_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eqasim_sa.ports.simulator import SimulationError

    current = settings(tmp_path)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_OMIT", "travel")
    with pytest.raises(SimulationError) as missing:
        simulator(current).simulate(
            load_config(current.config_path, 42), 42, current.workdir
        )
    assert "travel" in str(missing.value)
    assert "spy stdout" in missing.value.log_path.read_text()
    monkeypatch.delenv("EQASIM_SPY_OMIT")
    monkeypatch.setenv("EQASIM_SPY_FAIL", "7")
    with pytest.raises(SimulationError, match="code 7"):
        simulator(current).simulate(
            load_config(current.config_path, 42), 42, current.workdir
        )


def test_refuses_unrepresentable_override_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    resolved = load_config(current.config_path, 42)
    resolved["config"]["departments"] = [49]
    with pytest.raises(TypeError, match="departments"):
        simulator(current).simulate(resolved, 42, current.workdir)
    assert not record.exists()


def test_type_change_is_not_hidden_by_python_equality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    resolved = load_config(current.config_path, 42)
    resolved["config"]["sampling_rate"] = False
    with pytest.raises(TypeError, match="sampling_rate"):
        simulator(current).simulate(resolved, 42, current.workdir)
    assert not record.exists()


def test_process_launch_failure_has_recoverable_log(tmp_path: Path) -> None:
    current = settings(tmp_path)
    broken = EqasimSettings(
        current.config_path,
        current.eqasim_root,
        (str(tmp_path / "missing-command"),),
        current.campaign,
        current.scratch,
        current.context_hash,
    )
    from eqasim_sa.ports.simulator import SimulationError

    with pytest.raises(SimulationError, match="launch failed") as failure:
        simulator(broken).simulate(
            load_config(current.config_path, 42), 42, broken.workdir
        )
    assert "process launch failed" in failure.value.log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "case", ["nonsequential-index", "duplicate-empty-header", "malformed-csv"]
)
def test_rejects_invalid_analysis_csv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from eqasim_sa.ports.simulator import SimulationError

    current = settings(tmp_path)
    before = current.config_path.read_bytes()
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_CASE", case)
    with pytest.raises(SimulationError):
        simulator(current).simulate(
            load_config(current.config_path, 42), 42, current.workdir
        )
    assert current.config_path.read_bytes() == before


def test_analysis_csv_streams_multiple_batches_with_global_anonymous_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    output, normalized = attempt / "output", attempt / "normalized"
    output.mkdir(parents=True)
    normalized.mkdir()
    values = [f"value-{index:03d}" for index in range(200)]
    source = output / "age.csv"
    source.write_text(
        ",value\n"
        + "".join(f"{index},{value}\n" for index, value in enumerate(values)),
        encoding="utf-8",
    )
    monkeypatch.setattr(artifacts_module, "CSV_BLOCK_SIZE", 64)

    artifact = artifacts_module._normalize(
        source, output, normalized, "age", ",", True
    )
    parquet = pq.ParquetFile(artifact.path)

    assert parquet.metadata.num_row_groups > 1
    table = parquet.read()
    assert table.column_names == ["value"]
    assert table["value"].to_pylist() == values


def test_cross_batch_anonymous_index_failure_removes_partial_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    output, normalized = attempt / "output", attempt / "normalized"
    output.mkdir(parents=True)
    normalized.mkdir()
    indices = list(range(200))
    indices[150] = 0
    source = output / "age.csv"
    source.write_text(
        ",value\n" + "".join(f"{index},x\n" for index in indices),
        encoding="utf-8",
    )
    monkeypatch.setattr(artifacts_module, "CSV_BLOCK_SIZE", 64)

    with pytest.raises(ValueError, match="anonymous index is not sequential"):
        artifacts_module._normalize(
            source, output, normalized, "age", ",", True
        )

    assert not (normalized / "age.parquet").exists()


def test_streaming_writer_failure_removes_partial_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    output, normalized = attempt / "output", attempt / "normalized"
    output.mkdir(parents=True)
    normalized.mkdir()
    source = output / "persons.csv"
    source.write_text(
        "id;value\n" + "".join(f"{index};x\n" for index in range(200)),
        encoding="utf-8",
    )
    monkeypatch.setattr(artifacts_module, "CSV_BLOCK_SIZE", 64)
    real_writer = artifacts_module.pq.ParquetWriter

    class FailingWriter:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.writer = real_writer(*args, **kwargs)
            self.writes = 0

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args):  # type: ignore[no-untyped-def]
            self.writer.close()

        def write_batch(self, batch):  # type: ignore[no-untyped-def]
            self.writes += 1
            if self.writes == 2:
                raise OSError("injected writer failure")
            self.writer.write_batch(batch)

    monkeypatch.setattr(artifacts_module.pq, "ParquetWriter", FailingWriter)

    with pytest.raises(OSError, match="injected writer failure"):
        artifacts_module._normalize(
            source, output, normalized, "persons", ";", False
        )

    assert not (normalized / "persons.parquet").exists()


def test_rejects_output_symlink_escaping_attempt(tmp_path: Path) -> None:
    output = tmp_path / "output"
    normalized = tmp_path / "normalized"
    output.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("id;label\n1;x\n", encoding="utf-8")
    make_symlink(output / "nantes_persons.csv", outside)
    with pytest.raises(ValueError, match="escapes"):
        normalize_outputs(output, normalized, "nantes_")


def test_rejects_output_csv_symlink_even_when_target_stays_inside_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    target = output / "real.csv"
    target.write_text("id;label\n1;x\n", encoding="utf-8")
    make_symlink(output / "nantes_persons.csv", target)
    with pytest.raises(ValueError, match="symlink"):
        normalize_outputs(
            output, tmp_path / "normalized", "nantes_", ("synthesis.output",)
        )


def test_rejects_removed_and_reserved_config_keys_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    removed = load_config(current.config_path, 42)
    del removed["config"]["sampling_rate"]
    with pytest.raises(ValueError, match="removes YAML keys"):
        simulator(current).simulate(removed, 42, current.workdir)
    reserved = load_config(current.config_path, 42)
    reserved["config"]["run"] = "other.stage"
    with pytest.raises(ValueError, match="dedicated option"):
        simulator(current).simulate(reserved, 42, current.workdir)
    assert not record.exists()


def test_rejects_added_null_override_before_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    resolved = load_config(current.config_path, 42)
    resolved["config"]["new_parameter"] = None
    with pytest.raises(TypeError, match="new_parameter"):
        simulator(current).simulate(resolved, 42, current.workdir)
    assert not record.exists()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("[]\n", "root"),
        ("working_directory: 2\nrun: []\nconfig: {}\n", "working_directory"),
        ("working_directory: /x\nrun: x\nconfig: {}\n", "run"),
        ("working_directory: /x\nrun: []\nconfig: []\n", "config"),
    ],
)
def test_rejects_invalid_yaml_structures(
    tmp_path: Path, document: str, message: str
) -> None:
    config = tmp_path / "invalid.yml"
    config.write_text(document, encoding="utf-8")
    with pytest.raises(TypeError, match=message):
        load_config(config, 42)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            "working_directory: /x\nrun: []\nconfig:\n"
            "  sampling_rate: 0.1\n  sampling_rate: 0.2\n",
            "duplicate YAML key",
        ),
        (
            "working_directory: /x\nrun: []\nshared: &shared [44]\n"
            "config:\n  departments: *shared\n",
            "aliases",
        ),
    ],
)
def test_rejects_yaml_duplicates_and_aliases(
    tmp_path: Path, document: str, message: str
) -> None:
    config = tmp_path / "ambiguous.yml"
    config.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(config, 42)


def test_rejects_nul_invocation_and_override(tmp_path: Path) -> None:
    current = settings(tmp_path)
    with pytest.raises(ValueError, match="invocation"):
        EqasimSettings(
            current.config_path,
            current.eqasim_root,
            ("bad\0command",),
            current.campaign,
            current.scratch,
            current.context_hash,
        )
    resolved = load_config(current.config_path, 42)
    resolved["config"]["sampling_rate"] = "bad\0value"
    with pytest.raises(ValueError, match="NUL"):
        simulator(current).simulate(resolved, 42, current.workdir)


def test_rejects_workdir_symlink_escaping_scratch(tmp_path: Path) -> None:
    current = settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    current.workdir.parent.mkdir(parents=True)
    make_symlink(current.workdir, outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        simulator(current).simulate(
            load_config(current.config_path, 42), 42, current.workdir
        )


def test_rejects_output_directory_symlink_escaping_attempt(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "nantes_persons.csv").write_text("id;label\n1;x\n", encoding="utf-8")
    output = attempt / "output"
    make_symlink(output, outside, target_is_directory=True)
    with pytest.raises(ValueError, match="output directory escapes"):
        normalize_outputs(output, attempt / "normalized", "nantes_")


def option_value(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]
