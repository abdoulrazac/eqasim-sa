import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import eqasim_sa.adapters.eqasim.artifacts as artifacts_module
import eqasim_sa.core.indicators as indicators_module
from eqasim_sa.adapters.eqasim.artifacts import (
    ANALYSIS,
    KNOWN_OUTPUTS,
    POPULATION,
    collect_outputs,
)
from eqasim_sa.adapters.eqasim.config import EqasimSettings, load_config
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator
from eqasim_sa.core.indicators import OutputDeclaration, read_csv_table
from eqasim_sa.ports.simulator import SimulationError
from tests.conftest import make_symlink, pid_alive

ALL_OUTPUTS = {
    name: OutputDeclaration(known.default_file(), known.separator)
    for name, known in KNOWN_OUTPUTS.items()
    if name in (*POPULATION, *ANALYSIS)
}
PERSONS = {"persons": ALL_OUTPUTS["persons"]}


def write_yaml(path: Path, *, prefix: str = "nantes_") -> None:
    path.write_text(
        "working_directory: /ignored\n"
        "run: [synthesis.output, analysis.synthesis.population]\n"
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


def simulator(
    current: EqasimSettings, seed: int = 42, outputs=None  # type: ignore[no-untyped-def]
) -> EqasimSimulator:
    return EqasimSimulator(
        current,
        load_config(current.config_path, seed),
        outputs=ALL_OUTPUTS if outputs is None else outputs,
    )


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
        return EqasimSimulator(
            scoped, load_config(scoped.config_path, 42), outputs=ALL_OUTPUTS
        )

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


def test_loads_yaml_read_only_and_keeps_its_run_list(tmp_path: Path) -> None:
    config = tmp_path / "config.yml"
    write_yaml(config)
    config.write_text(
        config.read_text().replace(
            "run: [synthesis.output, analysis.synthesis.population]",
            "run: [user.stage, {synthesis.output: {x: 1}}, user.stage]",
        )
    )
    before = hashlib.sha256(config.read_bytes()).hexdigest()
    resolved = load_config(config, 42)
    assert resolved["run"] == ["user.stage", {"synthesis.output": {"x": 1}}, "user.stage"]
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


def test_real_spy_subprocess_collects_exact_declared_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    before = current.config_path.read_bytes()
    resolved = load_config(current.config_path, 42)
    adapter = EqasimSimulator(current, resolved, before, outputs=ALL_OUTPUTS)
    current.config_path.write_text("config:\n  replaced: true\n", encoding="utf-8")
    manifest = adapter.simulate(resolved, 42, current.workdir)
    assert {
        (artifact.name, artifact.path.name, artifact.source_path)
        for artifact in manifest.artifacts
    } == {
        (name, f"{name}.csv", declaration.source_path("nantes_"))
        for name, declaration in ALL_OUTPUTS.items()
    }
    observed = json.loads(record.read_text())
    assert observed["pid"] != os.getpid()
    assert observed["cwd"] == str(tmp_path)
    attempt = manifest.attempt_directory
    assert observed["argv"] == [
        str(attempt / "config.yml"),
        "--working-directory",
        str(current.workspace(0).cache),
        "--random_seed",
        "42",
        "--output_path",
        str(attempt / "output"),
        "--output_prefix",
        "nantes_",
    ]
    assert current.config_path.read_bytes() != before
    assert (attempt / "config.yml").read_bytes() == before
    age_path = next(a.path for a in manifest.artifacts if a.name == "age")
    assert age_path.read_bytes() == (
        attempt / "output" / "analysis_population" / "nantes_age.csv"
    ).read_bytes()
    age = read_csv_table(age_path, ",", "age")
    assert age.column_names == [
        "Age class",
        "HTS adjust with INSEE population weights",
        "Proportion_EQASIM",
    ]
    purpose = read_csv_table(
        next(a.path for a in manifest.artifacts if a.name == "age_purpose"),
        ",",
        "age_purpose",
    )
    assert purpose.column_names == ["age_class", "Home<>Work"]
    assert age["Age class"].to_pylist() == ["65+", "(99.0, inf]"]
    assert age["HTS adjust with INSEE population weights"].to_pylist() == ["0.5", ""]
    assert {path.name for path in (attempt / "collected").iterdir()} == {
        artifact.path.name for artifact in manifest.artifacts
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
        outputs=ALL_OUTPUTS,
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
        outputs=ALL_OUTPUTS,
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


def test_collects_only_declared_outputs_and_passes_no_run_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    resolved = load_config(current.config_path, 42)
    population = {
        name: ALL_OUTPUTS[name]
        for name in (
            "persons",
            "households",
            "activities",
            "trips",
            "vehicle_types",
            "vehicles",
        )
    }
    manifest = simulator(current, outputs=population).simulate(
        resolved, 42, current.workdir
    )
    assert {artifact.name for artifact in manifest.artifacts} == set(population)
    arguments = json.loads(record.read_text())["argv"]
    assert "--run" not in arguments
    assert "analysis.synthesis.population" not in arguments


@pytest.mark.parametrize("stages", [[], ["unknown.stage"]])
def test_rejects_run_that_differs_from_the_config_before_preflight(
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


def test_simulator_requires_declared_outputs(tmp_path: Path) -> None:
    current = settings(tmp_path)
    resolved = load_config(current.config_path, 42)
    with pytest.raises(ValueError, match="at least one declared output"):
        EqasimSimulator(current, resolved, outputs={})
    with pytest.raises(ValueError, match="output name"):
        EqasimSimulator(current, resolved, outputs={"a.b": ALL_OUTPUTS["age"]})
    assert not hasattr(EqasimSimulator, "requirements")


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
def test_unusual_analysis_csv_is_archived_as_is_and_judged_when_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    current = settings(tmp_path)
    before = current.config_path.read_bytes()
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_CASE", case)
    manifest = simulator(current).simulate(
        load_config(current.config_path, 42), 42, current.workdir
    )
    age = next(a.path for a in manifest.artifacts if a.name == "age")
    assert age.read_bytes() == (
        manifest.attempt_directory
        / "output"
        / "analysis_population"
        / "nantes_age.csv"
    ).read_bytes()
    if case == "nonsequential-index":
        assert read_csv_table(age, ",", "age").to_pylist() == [{"Age class": "65+"}]
    else:
        with pytest.raises(ValueError, match="output 'age'"):
            read_csv_table(age, ",", "age")
    assert current.config_path.read_bytes() == before


def test_analysis_csv_streams_multiple_blocks_and_drops_the_anonymous_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = [f"value-{index:03d}" for index in range(200)]
    source = tmp_path / "age.csv"
    source.write_text(
        ",value\n"
        + "".join(f"{index},{value}\n" for index, value in enumerate(values)),
        encoding="utf-8",
    )
    monkeypatch.setattr(indicators_module, "CSV_BLOCK_SIZE", 64)

    table = read_csv_table(source, ",", "age")

    assert table.column_names == ["value"]
    assert table["value"].to_pylist() == values


def test_anonymous_index_values_are_not_interpreted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    indices = list(range(200))
    indices[150] = 0
    source = tmp_path / "age.csv"
    source.write_text(
        ",value\n" + "".join(f"{index},x\n" for index in indices),
        encoding="utf-8",
    )
    monkeypatch.setattr(indicators_module, "CSV_BLOCK_SIZE", 64)

    table = read_csv_table(source, ",", "age")

    assert table.column_names == ["value"]
    assert table.num_rows == 200


def test_copy_failure_removes_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "output"
    output.mkdir(parents=True)
    (output / "nantes_persons.csv").write_text(
        "id;value\n" + "".join(f"{index};x\n" for index in range(200)),
        encoding="utf-8",
    )
    real_copy = artifacts_module.shutil.copyfileobj

    def failing_copy(source, target, length=0):  # type: ignore[no-untyped-def]
        target.write(source.read(10))
        raise OSError("injected copy failure")

    monkeypatch.setattr(artifacts_module.shutil, "copyfileobj", failing_copy)
    with pytest.raises(OSError, match="injected copy failure"):
        collect_outputs(output, attempt / "collected", "nantes_", PERSONS)
    assert not (attempt / "collected" / "persons.csv").exists()
    monkeypatch.setattr(artifacts_module.shutil, "copyfileobj", real_copy)


def test_rejects_output_symlink_escaping_attempt(tmp_path: Path) -> None:
    output = tmp_path / "output"
    collected = tmp_path / "collected"
    output.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("id;label\n1;x\n", encoding="utf-8")
    make_symlink(output / "nantes_persons.csv", outside)
    with pytest.raises(ValueError, match="symlink escapes"):
        collect_outputs(output, collected, "nantes_", PERSONS)
    nested = {"age": ALL_OUTPUTS["age"]}
    make_symlink(output / "analysis_population", tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink escapes"):
        collect_outputs(output, tmp_path / "again", "nantes_", nested)


def test_rejects_output_csv_symlink_even_when_target_stays_inside_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    target = output / "real.csv"
    target.write_text("id;label\n1;x\n", encoding="utf-8")
    make_symlink(output / "nantes_persons.csv", target)
    with pytest.raises(ValueError, match="symlink"):
        collect_outputs(output, tmp_path / "collected", "nantes_", PERSONS)


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
        ("working_directory: /x\nrun: []\nconfig: {}\n", "run"),
        ("working_directory: /x\nrun: [{a: 1, b: 2}]\nconfig: {}\n", "run"),
        ("working_directory: /x\nrun: [a]\nconfig: []\n", "config"),
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
        collect_outputs(output, attempt / "collected", "nantes_", PERSONS)


def option_value(arguments: list[str], name: str) -> str:
    return arguments[arguments.index(name) + 1]


def test_fake_pipeline_interrupt_leaves_slot_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    current = settings(tmp_path)
    record, child = tmp_path / "record.json", tmp_path / "child.pid"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    monkeypatch.setenv("EQASIM_SPY_CHILD_RECORD", str(child))
    monkeypatch.setenv("EQASIM_SPY_SLEEP", "60")
    first = simulator(current)
    outcome: list[str] = []

    def run() -> None:
        try:
            first.simulate(load_config(current.config_path, 42), 42, current.workdir)
        except KeyboardInterrupt:
            outcome.append("interrupted")

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 15
    while not (child.exists() and record.exists()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child.exists() and record.exists()
    started = time.monotonic()
    first.interrupt()
    thread.join(timeout=15)
    assert not thread.is_alive() and outcome == ["interrupted"]
    assert time.monotonic() - started < 15
    for pid in (
        json.loads(record.read_text(encoding="utf-8"))["pid"],
        int(child.read_text(encoding="utf-8")),
    ):
        assert not pid_alive(pid)
    assert current.workspace(0).status() == "active"

    monkeypatch.delenv("EQASIM_SPY_CHILD_RECORD")
    monkeypatch.delenv("EQASIM_SPY_SLEEP")
    second = simulator(current)
    manifest = second.simulate(
        load_config(current.config_path, 42), 42, current.workdir
    )
    assert manifest.artifacts
    assert current.workspace(0).status() == "clean"


def test_windows_resolution_searches_sealed_path_with_sealed_pathext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters import execution_seal
    from eqasim_sa.utils import platform

    launch = tmp_path / "launch"
    tools = tmp_path / "root" / "tools"
    launch.mkdir()
    tools.mkdir(parents=True)
    (launch / "uv.EXE").write_bytes(b"decoy")
    (launch / "uv").write_bytes(b"decoy")
    tool = tools / "uv.EXE"
    tool.write_bytes(b"MZ")
    (tools / "uv.BAT").write_bytes(b"later suffix")
    monkeypatch.chdir(launch)
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".BAT")
    root = tmp_path / "root"
    sealed = {"PATH": "tools", "PATHEXT": ".COM;.EXE;.BAT"}

    resolved = execution_seal.resolve_invocation(["uv", "run"], root, sealed)
    assert resolved == (str(tool.resolve()), "run")
    explicit = execution_seal.resolve_invocation(["uv.BAT"], root, sealed)
    assert explicit == (str((tools / "uv.BAT").resolve()),)
    relative = execution_seal.resolve_invocation(["tools/uv"], root, sealed)
    assert relative == (str(tool.resolve()),)
    for environment in ({"PATH": "tools", "PATHEXT": ".CMD"}, {"PATH": ""}):
        with pytest.raises(FileNotFoundError, match="not resolvable"):
            execution_seal.resolve_invocation(["uv"], root, environment)
    with pytest.raises(FileNotFoundError, match="not resolvable"):
        execution_seal.resolve_invocation(["uv"], launch, {"PATH": str(tools.parent / "none")})


@pytest.mark.windows_only
def test_resolve_invocation_finds_exe_without_suffix(tmp_path: Path) -> None:
    from eqasim_sa.adapters.execution_seal import resolve_invocation

    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv.exe").write_bytes(b"MZ")
    resolved = resolve_invocation(["uv", "run"], tmp_path, {"PATH": str(tools)})
    assert resolved[0].lower().endswith("uv.exe")
    assert resolved[1:] == ("run",)


def test_probe_imports_a_fake_synpp_module_through_the_invocation(
    tmp_path: Path,
) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    (tmp_path / "synpp.py").write_text("print('noise that is never read')\n")
    assert probe_environment((sys.executable, "-m", "synpp"), tmp_path) is None


def test_probe_never_reads_synpp_output_only_the_exit_code(tmp_path: Path) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    (tmp_path / "synpp.py").write_text(
        "import sys\nprint('Config file does not exist: x')\nsys.exit(3)\n"
    )
    with pytest.raises(RuntimeError, match="cannot import synpp.*returncode=3"):
        probe_environment((sys.executable, "-m", "synpp"), tmp_path)


def test_probe_fails_when_the_interpreter_has_no_synpp(tmp_path: Path) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    code = "import sys; sys.modules['synpp'] = None"
    with pytest.raises(RuntimeError, match="environment probe failed"):
        probe_environment(
            (sys.executable, "-c", f"{code}\nimport synpp", "-m", "synpp"), tmp_path
        )


def test_probe_names_a_missing_invocation(tmp_path: Path) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    with pytest.raises(RuntimeError, match="no-such-python-xyz"):
        probe_environment(("no-such-python-xyz", "-m", "synpp"), tmp_path)


def test_probe_is_skipped_without_trailing_m_synpp(tmp_path: Path) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    assert (
        probe_environment(("uv", "run", "synpp"), tmp_path)
        == "synpp probe skipped: invocation does not end with -m synpp"
    )


def test_probe_timeout_is_not_swallowed(tmp_path: Path) -> None:
    import subprocess
    import time

    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        probe_environment(
            (sys.executable, "-c", "import time; time.sleep(10)", "-m", "synpp"),
            tmp_path,
            timeout=0.05,
        )
    assert time.monotonic() - started < 1


def test_probe_on_the_real_synpp(tmp_path: Path) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    pytest.importorskip("synpp")
    assert probe_environment((sys.executable, "-m", "synpp"), tmp_path) is None


def test_probe_does_not_inherit_the_virtual_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters.eqasim.simulator import probe_environment

    (tmp_path / "synpp.py").write_text(
        "import os, sys\nsys.exit(4 if 'VIRTUAL_ENV' in os.environ else 0)\n"
    )
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "eqasim-sa-venv"))
    assert probe_environment((sys.executable, "-m", "synpp"), tmp_path) is None


def test_eqasim_does_not_inherit_the_virtual_env_without_a_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = settings(tmp_path)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "eqasim-sa-venv"))
    adapter = simulator(current)

    adapter.simulate(load_config(current.config_path, 42), 42, current.workdir)

    assert json.loads(record.read_text())["virtualEnv"] is None
