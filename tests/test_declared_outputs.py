"""Declared eqasim outputs: defaults, run: list, byte-for-byte archives, CSV reading."""

import hashlib
import json
import math
import sys
from pathlib import Path

import pyarrow as pa
import pytest
import yaml

from eqasim_sa.adapters.application import (
    analyze_from_meta_config,
    plan_from_meta_config,
    run_from_meta_config,
    show_config_from_meta_config,
)
from eqasim_sa.adapters.campaign_design import _design
from eqasim_sa.adapters.data_manifest import generate_data_manifest
from eqasim_sa.adapters.eqasim.config import EqasimSettings, load_config
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters.meta_config_defaults import resolve_meta_config_document
from eqasim_sa.core.indicators import (
    ArtifactSet,
    OutputDeclaration,
    TableArtifact,
    TotalVariationIndicator,
    read_csv_table,
)
from eqasim_sa.core.ledger import RunRecord, completion_state
from eqasim_sa.core.plan import load_run_specs
from eqasim_sa.core.run import RunSpec, compute_run_id, execute_run
from eqasim_sa.ports.simulator import SimulationError

FIXTURES = Path(__file__).with_name("fixtures") / "eqasim-output"
SPY = Path(__file__).with_name("eqasim_spy.py")
BOTH_STAGES = ["synthesis.output", "analysis.synthesis.population"]


def project(
    tmp_path: Path,
    outputs: object,
    *,
    run: list | None = None,
    extra: str = "",
) -> tuple[Path, Path]:
    """Write an eqasim folder, its config and a grid meta-config."""

    model = tmp_path / "model"
    model.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "input.txt").write_text("sealed data\n")
    config = model / "config.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "working_directory": "cache",
                "run": BOTH_STAGES if run is None else run,
                "config": {
                    "sampling_rate": 0.001,
                    "data_path": "../data",
                    "processes": 1,
                    "random_seed": 9,
                    "output_prefix": "nantes_",
                },
            },
            sort_keys=False,
        )
    )
    (model / "uv.lock").write_text("locked\n")
    generate_data_manifest(data, tmp_path / "manifest.json")
    document = {
        "campaign": "demo",
        "campaign_root": "campaign",
        "model": {
            "root": "model",
            "config": "config.yml",
            "data_manifest": "manifest.json",
        },
        "experiment": {"method": "grid", "seeds": [41]},
        "parameters": {"sampling_rate": [0.001]},
    }
    if outputs is not None:
        document["outputs"] = outputs
    meta = tmp_path / "meta.yml"
    meta.write_text(yaml.safe_dump(document, sort_keys=False) + extra)
    return meta, config


def add_spy_execution(tmp_path: Path, meta: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    meta.write_text(
        meta.read_text()
        + "execution:\n  workers: 1\n"
        + f"  scratch: {json.dumps(str(scratch))}\n"
        + f"  invocation: {json.dumps([sys.executable, str(SPY)])}\n"
    )


def spy_settings(tmp_path: Path, run: list) -> EqasimSettings:
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "working_directory": "/ignored",
                "run": run,
                "config": {
                    "sampling_rate": 0.001,
                    "output_prefix": "nantes_",
                    "processes": 1,
                },
            },
            sort_keys=False,
        )
    )
    (tmp_path / "scratch").mkdir()
    return EqasimSettings(
        config_path=config,
        eqasim_root=tmp_path,
        invocation=(sys.executable, str(SPY)),
        campaign="nantes.v1",
        scratch=tmp_path / "scratch",
        context_hash="c" * 64,
    )


def declared(**files: str) -> dict[str, OutputDeclaration]:
    return {
        name: OutputDeclaration(file, ";" if file.endswith("persons.csv") else ",")
        for name, file in files.items()
    }


def test_known_output_defaults_are_resolved_and_shown(tmp_path: Path) -> None:
    resolved = resolve_meta_config_document(
        {
            "outputs": {
                "age": {"category": "Age class"},
                "persons": {},
                "my_metrics": {"file": "analysis_population/{prefix}my.csv"},
            }
        }
    )
    assert resolved["outputs"] == {
        "age": {
            "category": "Age class",
            "file": "analysis_population/{prefix}age.csv",
            "separator": ",",
        },
        "persons": {"file": "{prefix}persons.csv", "separator": ";"},
        "my_metrics": {
            "file": "analysis_population/{prefix}my.csv",
            "separator": ",",
        },
    }
    meta, _ = project(tmp_path, {"age": {}, "persons": {}})
    loaded = load_meta_config(meta, verify_data=False)
    assert loaded.outputs == {
        "age": OutputDeclaration("analysis_population/{prefix}age.csv", ","),
        "persons": OutputDeclaration("{prefix}persons.csv", ";"),
    }
    shown = yaml.safe_load(show_config_from_meta_config(meta))
    assert shown["outputs"] == {
        "age": {"file": "analysis_population/{prefix}age.csv", "separator": ","},
        "persons": {"file": "{prefix}persons.csv", "separator": ";"},
    }


@pytest.mark.parametrize(
    ("outputs", "message"),
    [
        ({"my_metrics": {}}, "output 'my_metrics' is not a known eqasim output; declare its file"),
        ({"a.b": {"file": "a.csv"}}, "output name 'a.b'"),
        ({"x": {"file": "../x.csv"}}, "output 'x' file"),
        ({"x": {"file": "C:\\x"}}, "output 'x' file"),
        ({"x": {"file": "/x"}}, "output 'x' file"),
        ({"x": {"file": "{other}x.csv"}}, "output 'x' file"),
        ({"x": {"file": "x.csv", "separator": ";;"}}, "output 'x' separator"),
        ({"age": {"columns": ["a"]}}, "output 'age' keys must be among"),
    ],
    ids=["unknown", "dot", "parent", "backslash", "absolute", "template", "sep", "key"],
)
def test_unknown_output_requires_file_and_names_are_validated(
    tmp_path: Path, outputs: dict, message: str
) -> None:
    meta, _ = project(tmp_path, outputs)
    with pytest.raises((TypeError, ValueError)) as error:
        load_meta_config(meta, verify_data=False)
    assert message in str(error.value)


@pytest.mark.parametrize("outputs", [None, {}], ids=["absent", "empty"])
def test_outputs_must_not_be_empty(tmp_path: Path, outputs: object) -> None:
    meta, _ = project(tmp_path, outputs)
    with pytest.raises(ValueError, match="outputs must declare at least one output"):
        load_meta_config(meta, verify_data=False)


def test_default_known_output_whose_stage_is_not_in_run_is_rejected(
    tmp_path: Path,
) -> None:
    meta, config = project(
        tmp_path, {"persons": {}, "age": {}}, run=["synthesis.output"]
    )
    with pytest.raises(ValueError) as error:
        load_meta_config(meta, verify_data=False)
    assert str(error.value) == (
        "output 'age' is written by eqasim stage 'analysis.synthesis.population', "
        f"which is not in run: of {config.resolve()}; "
        "add it to run: in your eqasim config"
    )


def test_output_whose_archive_name_is_invalid_is_rejected_at_load(
    tmp_path: Path,
) -> None:
    meta, _ = project(tmp_path, {"age": {"file": "analysis_population/{prefix}age.csv~"}})
    with pytest.raises(ValueError) as error:
        load_meta_config(meta, verify_data=False)
    assert str(error.value) == (
        "output 'age' file suffix cannot name an archive file: "
        "'analysis_population/nantes_age.csv~'"
    )


def _set_output_formats(config: Path, formats: list[str]) -> None:
    document = yaml.safe_load(config.read_text())
    document["config"]["output_formats"] = formats
    config.write_text(yaml.safe_dump(document, sort_keys=False))


@pytest.mark.parametrize(
    ("outputs", "formats", "message"),
    [
        (
            {"persons": {}},
            ["parquet", "gpkg"],
            "output 'persons' is a CSV file but output_formats of {config} "
            "is ['parquet', 'gpkg']; add csv to output_formats",
        ),
        (
            {"activities_spatial": {"file": "{prefix}activities.gpkg"}},
            ["csv"],
            "output 'activities_spatial' is a GeoPackage file but output_formats "
            "of {config} is ['csv']; add gpkg to output_formats",
        ),
    ],
    ids=["csv", "gpkg"],
)
def test_output_missing_from_output_formats_is_rejected(
    tmp_path: Path, outputs: dict, formats: list, message: str
) -> None:
    meta, config = project(tmp_path, outputs)
    _set_output_formats(config, formats)
    with pytest.raises(ValueError) as error:
        load_meta_config(meta, verify_data=False)
    assert str(error.value) == message.format(config=config.resolve())


def test_output_formats_listing_the_format_loads(tmp_path: Path) -> None:
    meta, config = project(
        tmp_path,
        {"persons": {}, "age": {}, "spatial": {"file": "{prefix}activities.gpkg"}},
    )
    _set_output_formats(config, ["csv", "gpkg"])
    assert set(load_meta_config(meta, verify_data=False).outputs) == {
        "persons",
        "age",
        "spatial",
    }


@pytest.mark.parametrize(
    "indicator",
    [
        {"share": {"output": "spatial", "category": "a"}},
        {"value": {"output": "spatial", "column": "a"}},
        {"tv_distance": {"output": "spatial"}},
    ],
    ids=["share", "value", "tv_distance"],
)
def test_indicator_on_a_non_csv_output_is_rejected_at_load(
    tmp_path: Path, indicator: dict
) -> None:
    spatial = {
        "file": "{prefix}activities.gpkg",
        "category": "a",
        "simulated": "b",
        "references": ["c"],
    }
    kind = next(iter(indicator))
    meta, _ = project(
        tmp_path,
        {"spatial": spatial},
        extra=yaml.safe_dump({"indicators": [indicator]}, sort_keys=False),
    )
    with pytest.raises(ValueError) as error:
        load_meta_config(meta, verify_data=False)
    assert str(error.value) == (
        f"indicator 0.{kind} reads output 'spatial', whose file "
        "'{prefix}activities.gpkg' is not a .csv file; indicators read CSV tables only"
    )


def test_overridden_file_skips_the_stage_check(tmp_path: Path) -> None:
    meta, _ = project(
        tmp_path,
        {"age": {"file": "custom/{prefix}age.csv"}, "my": {"file": "my.csv"}},
        run=[{"my.stage": {"x": 1}}],
    )
    loaded = load_meta_config(meta, verify_data=False)
    assert loaded.outputs["age"].file == "custom/{prefix}age.csv"
    # A known default stage given as a {stage: parameters} item counts as in run:.
    meta, _ = project(
        tmp_path / "second",
        {"age": {}},
        run=["synthesis.output", {"analysis.synthesis.population": {"x": 1}}],
    )
    assert load_meta_config(meta, verify_data=False).outputs["age"].separator == ","


@pytest.mark.parametrize(
    "run",
    [
        [],
        "synthesis.output",
        [""],
        [3],
        [{"a": 1, "b": 2}],
        [{"": 1}],
    ],
    ids=["empty", "string", "blank", "number", "two-keys", "blank-key"],
)
def test_run_list_items_are_validated(tmp_path: Path, run: object) -> None:
    meta, _ = project(tmp_path, {"x": {"file": "x.csv"}}, run=run)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError), match="run"):
        load_meta_config(meta, verify_data=False)


def test_run_list_reaches_synpp_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = [
        "synthesis.output",
        "user.unknown_stage",
        "synthesis.output",
        {"analysis.synthesis.population": {"option": 1}},
    ]
    meta, _ = project(tmp_path / "meta", {"persons": {}, "age": {}}, run=run)
    loaded = load_meta_config(meta, verify_data=False)
    design = _design(loaded)
    assert design.requirements == {}
    plan_from_meta_config(meta)
    (spec,) = load_run_specs(
        loaded.campaign_root / "plan.parquet", design, loaded.campaign
    )
    assert spec.resolved_config["run"] == run

    current = spy_settings(tmp_path, run)
    record = tmp_path / "record.json"
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(record))
    resolved = load_config(current.config_path, 42)
    assert resolved["run"] == run
    adapter = EqasimSimulator(
        current, resolved, outputs=declared(persons="{prefix}persons.csv")
    )
    manifest = adapter.simulate(resolved, 42, current.workdir)
    snapshot = yaml.safe_load(
        (manifest.attempt_directory / "config.yml").read_text()
    )
    argv = json.loads(record.read_text())["argv"]
    assert snapshot["run"] == run
    assert "--run" not in argv


def test_declared_outputs_archived_byte_for_byte_with_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = spy_settings(tmp_path, BOTH_STAGES)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    resolved = load_config(current.config_path, 42)
    adapter = EqasimSimulator(
        current,
        resolved,
        outputs=declared(
            age="analysis_population/{prefix}age.csv", persons="{prefix}persons.csv"
        ),
    )
    produced: dict[str, bytes] = {}

    class Capturing:
        def simulate(self, config, seed, workdir):  # type: ignore[no-untyped-def]
            manifest = adapter.simulate(config, seed, workdir)
            output = manifest.attempt_directory / "output"
            for artifact in manifest.artifacts:
                produced[artifact.name] = (output / artifact.source_path).read_bytes()
                assert artifact.path.read_bytes() == produced[artifact.name]
            return manifest

    spec = RunSpec("nantes.v1", resolved, 42, "c" * 64, {})
    campaign = tmp_path / "campaign"
    result = execute_run(spec, Capturing(), campaign, adapter.workspace.root)
    assert result.status == "completed", result.error
    run_id = compute_run_id(resolved, 42)
    archive = campaign / "archives" / run_id
    assert sorted(path.name for path in archive.iterdir()) == ["age.csv", "persons.csv"]
    assert (archive / "age.csv").read_bytes() == produced["age"]
    assert (archive / "persons.csv").read_bytes() == produced["persons"]
    completion = completion_state(
        campaign, RunRecord(run_id, "nantes.v1", 42, "c" * 64, "", 0.0, {})
    )
    assert completion is not None
    assert {
        (entry.name, entry.source_path, entry.size, entry.sha256)
        for entry in completion.artifacts
    } == {
        (
            name,
            source,
            len(produced[name]),
            hashlib.sha256(produced[name]).hexdigest(),
        )
        for name, source in (
            ("age", "analysis_population/nantes_age.csv"),
            ("persons", "nantes_persons.csv"),
        )
    }


def test_gpkg_output_is_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = spy_settings(tmp_path, BOTH_STAGES)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    resolved = load_config(current.config_path, 42)
    adapter = EqasimSimulator(
        current,
        resolved,
        outputs={"activities_spatial": OutputDeclaration("{prefix}activities.gpkg", ",")},
    )
    spec = RunSpec("nantes.v1", resolved, 42, "c" * 64, {})
    campaign = tmp_path / "campaign"
    result = execute_run(spec, adapter, campaign, adapter.workspace.root)
    assert result.status == "completed", result.error
    archive = campaign / "archives" / compute_run_id(resolved, 42)
    assert [path.name for path in archive.iterdir()] == ["activities_spatial.gpkg"]
    assert (archive / "activities_spatial.gpkg").read_bytes().startswith(b"GPKG\0")


def test_missing_declared_file_fails_run_with_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = spy_settings(tmp_path, ["synthesis.output"])
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_OMIT", "age")
    resolved = load_config(current.config_path, 42)
    adapter = EqasimSimulator(
        current,
        resolved,
        outputs=declared(age="analysis_population/{prefix}age.csv"),
    )
    with pytest.raises(SimulationError) as error:
        adapter.simulate(resolved, 42, current.workdir)
    assert str(error.value) == (
        "eqasim did not write declared output 'age' "
        "(expected analysis_population/nantes_age.csv); "
        "check that the stage producing it is in run: of your eqasim config"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"file": "custom/{prefix}age.csv"},
        {"separator": ";"},
    ],
    ids=["file", "separator"],
)
def test_changing_output_file_after_plan_refuses_resume(
    tmp_path: Path, change: dict
) -> None:
    meta, _ = project(tmp_path, {"age": {}})
    plan_from_meta_config(meta)
    document = yaml.safe_load(meta.read_text())
    document["outputs"]["age"].update(change)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    loaded = load_meta_config(meta, verify_data=False)
    (field,) = change
    with pytest.raises(ValueError) as error:
        load_run_specs(
            loaded.campaign_root / "plan.parquet", _design(loaded), loaded.campaign
        )
    assert str(error.value) == f"output 'age' changed since plan: {field}"
    plan_metadata = _design(loaded)
    assert plan_metadata.outputs["age"] == OutputDeclaration(
        change.get("file", "analysis_population/{prefix}age.csv"),
        change.get("separator", ","),
    )


def test_editing_eqasim_code_after_plan_names_the_source_change(
    tmp_path: Path,
) -> None:
    meta, config = project(tmp_path, {"age": {}})
    (config.parent / "synthesis.py").write_text("x = 1\n")
    plan_from_meta_config(meta)
    (config.parent / "synthesis.py").write_text("x = 2\n")
    loaded = load_meta_config(meta, verify_data=False)
    with pytest.raises(ValueError) as error:
        load_run_specs(
            loaded.campaign_root / "plan.parquet", _design(loaded), loaded.campaign
        )
    assert str(error.value) == (
        "eqasim source changed since plan (eqasimSourceSha256)"
    )


def test_adding_an_output_after_plan_names_the_output(tmp_path: Path) -> None:
    meta, _ = project(tmp_path, {"age": {}})
    plan_from_meta_config(meta)
    document = yaml.safe_load(meta.read_text())
    document["outputs"]["persons"] = {}
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    loaded = load_meta_config(meta, verify_data=False)
    with pytest.raises(ValueError) as error:
        load_run_specs(
            loaded.campaign_root / "plan.parquet", _design(loaded), loaded.campaign
        )
    assert str(error.value) == "output 'persons' added since plan"


def test_adding_a_reference_after_runs_does_not_block_analyze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = "HTS adjust with INSEE population weights"
    age = {"category": "Age class", "simulated": "Proportion_EQASIM", "references": []}
    meta, _ = project(tmp_path, {"age": age})
    add_spy_execution(tmp_path, meta)
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_CASE", "complete-references")
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    document = yaml.safe_load(meta.read_text())
    document["outputs"]["age"]["references"] = [reference]
    document["outputs"]["age"]["category"] = "Age class"
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    payload = analyze_from_meta_config(meta)

    assert payload["status"] == "completed"
    assert payload["indicators"] == 1


def _fixture_artifacts() -> ArtifactSet:
    return ArtifactSet(
        {
            "age": FIXTURES / "analysis_population" / "nantes_age.csv",
            "distance": FIXTURES / "analysis_population" / "nantes_distance.csv",
            "license": FIXTURES / "analysis_population" / "nantes_license.csv",
            "nbr_vehicle": FIXTURES / "analysis_population" / "nantes_nbr_vehicle.csv",
            "age_purpose": FIXTURES / "analysis_population" / "nantes_age_purpose.csv",
            "persons": FIXTURES / "nantes_persons.csv",
        }
    )


def _expected_tv(path: Path, synthetic: str, reference: str) -> float:
    lines = path.read_text().splitlines()
    header = lines[0].split(",")
    rows = [line.rsplit(",", len(header) - 2) for line in lines[1:]]
    left = [float(row[header.index(synthetic) - len(header)]) for row in rows]
    right = [float(row[header.index(reference) - len(header)]) for row in rows]
    left = [value / sum(left) for value in left]
    right = [value / sum(right) for value in right]
    return 0.5 * math.fsum(abs(a - b) for a, b in zip(left, right))


def test_tv_distance_reads_real_csvs() -> None:
    artifacts = _fixture_artifacts()
    age = TableArtifact(
        "age",
        "analysis_population/{prefix}age.csv",
        ",",
        "Age class",
        "Proportion_EQASIM",
        ("Proportion_INSEE",),
    )
    distance = TableArtifact(
        "distance",
        "analysis_population/{prefix}distance.csv",
        ",",
        "Distance class",
        "Proportion_EQASIM",
        ("Proportion_HTS",),
    )
    table = age.read(artifacts, None)
    assert table.column_names[0] == "Age class"
    assert "" not in table.column_names
    assert TotalVariationIndicator(age, "Proportion_INSEE").compute(
        artifacts
    ) == pytest.approx(
        _expected_tv(
            FIXTURES / "analysis_population" / "nantes_age.csv",
            "Proportion_EQASIM",
            "Proportion_INSEE",
        ),
        abs=1e-15,
    )
    labels = distance.read(artifacts, ("Distance class",))["Distance class"]
    assert labels.to_pylist()[:2] == ["(0.0, 1.0]", "(1.0, 2.0]"]
    value = TotalVariationIndicator(distance, "Proportion_HTS").compute(artifacts)
    assert 0.0 < value < 1.0


def test_csv_columns_are_read_as_text() -> None:
    artifacts = _fixture_artifacts()
    license_table = read_csv_table(artifacts.path("license"), ",", "license")
    vehicles = read_csv_table(artifacts.path("nbr_vehicle"), ",", "nbr_vehicle")
    persons = read_csv_table(artifacts.path("persons"), ";", "persons")
    purpose = read_csv_table(artifacts.path("age_purpose"), ",", "age_purpose")
    for table in (license_table, vehicles, persons, purpose):
        assert all(field.type == pa.string() for field in table.schema)
    assert license_table["Possession of license"].to_pylist() == ["False", "True"]
    assert vehicles["Number of vehicles class"].to_pylist() == ["0", "1", "2", "3+"]
    assert persons["has_driving_license"].to_pylist()[:2] == ["False", "True"]
    assert purpose.column_names[0] == "age_class"
    assert purpose["Home<>Work"].to_pylist()[3] == "63000.0"
