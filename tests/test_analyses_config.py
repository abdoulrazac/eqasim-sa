"""The ``analyses`` block declares analysis scripts read by indicators."""

import os
import sys
from pathlib import Path

import pytest
import yaml

from eqasim_sa.adapters.application import (
    analyze_from_meta_config,
    plan_from_meta_config,
    run_from_meta_config,
    show_config_from_meta_config,
)
from eqasim_sa.adapters.meta_config import load_meta_config
import eqasim_sa.adapters.project_init as project_init_module
from eqasim_sa.core.analyses import AnalysisDeclaration
from eqasim_sa.core.indicators import (
    INDICATOR_DEFINITION_DOMAIN,
    ArtifactSet,
    DerivedOutput,
    OutputDeclaration,
    ShareIndicator,
    TableArtifact,
    ValueIndicator,
    analysis_identity,
    indicator_definition_sha256,
    validate_indicator_definition,
)
from tests.conftest import make_symlink
from tests.test_sealed_analysis import REFERENCE, campaign

NO_SYNPP = (
    "analyses need execution.python because execution.invocation "
    "does not end with -m synpp"
)


def _edit(meta: Path, edit) -> None:  # type: ignore[no-untyped-def]
    document = yaml.safe_load(meta.read_text())
    edit(document)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))


def _script(tmp_path: Path, name: str = "commute.py") -> Path:
    folder = tmp_path / "analyses"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text("def compute(files, parameters):\n    raise NotImplementedError\n")
    return path


def _with_analysis(
    tmp_path: Path,
    analysis: dict | None = None,
    python: list[str] | None = None,
    method: str = "grid",
) -> Path:
    meta, _ = campaign(tmp_path, method)
    _script(tmp_path)

    def edit(document: dict) -> None:
        document["outputs"]["trips"] = {"file": "trips.csv", "separator": ";"}
        document["analyses"] = {
            "commute": analysis
            if analysis is not None
            else {"script": "analyses/commute.py", "inputs": ["trips", "age"]}
        }
        document["execution"]["python"] = (
            python if python is not None else [sys.executable]
        )

    _edit(meta, edit)
    return meta


def test_analyses_block_parses_and_shows_in_show_config(tmp_path: Path) -> None:
    meta = _with_analysis(
        tmp_path,
        {
            "script": "analyses/commute.py",
            "inputs": ["trips", "age"],
            "category": "mode",
            "simulated": "share",
        },
    )

    loaded = load_meta_config(meta, verify_data=False)

    assert set(loaded.outputs) == {"age", "trips"}
    declaration = loaded.analyses["commute"]
    assert isinstance(declaration, AnalysisDeclaration)
    assert declaration.name == "commute"
    assert declaration.script == (tmp_path / "analyses" / "commute.py").resolve()
    assert declaration.inputs == ("trips", "age")
    assert declaration.output == DerivedOutput(
        "commute", "commute", ("trips", "age"), ",", "mode", "share"
    )
    assert loaded.analysis_python == (sys.executable,)
    shown = yaml.safe_load(show_config_from_meta_config(meta))
    assert list(shown).index("analyses") == list(shown).index("outputs") + 1
    assert shown["analyses"]["commute"]["inputs"] == ["trips", "age"]


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("age", "analysis name 'age' is already an output name"),
        ("AGE", "differ only by case"),
        ("bad name", "letters, digits"),
        ("CON", "CON"),
        ("logs", "reserved"),
    ],
)
def test_analysis_name_collision_with_output_is_rejected(
    tmp_path: Path, name: str, message: str
) -> None:
    meta = _with_analysis(tmp_path)
    _edit(
        meta,
        lambda document: document.update(
            analyses={name: document["analyses"]["commute"]}
        ),
    )

    with pytest.raises(ValueError, match=message):
        load_meta_config(meta, verify_data=False)


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ([], "analysis 'commute' inputs must be a non-empty list of strings"),
        (["trips", "trips"], "must not contain duplicates"),
        (["missing"], "analysis 'commute' input 'missing' is not declared in outputs"),
        (["other"], "analysis 'commute' input 'other' is an analysis"),
        (["parquet"], "analysis 'commute' input 'parquet' file 'x.parquet' must be a .csv or .gpkg file"),
    ],
)
def test_analysis_inputs_must_be_declared_csv_or_gpkg_outputs(
    tmp_path: Path, inputs: list[str], message: str
) -> None:
    meta = _with_analysis(tmp_path)
    _script(tmp_path, "other.py")

    def edit(document: dict) -> None:
        document["outputs"]["parquet"] = {"file": "x.parquet"}
        document["outputs"]["zones"] = {"file": "zones.GPKG"}
        document["analyses"]["commute"]["inputs"] = inputs
        document["analyses"]["other"] = {
            "script": "analyses/other.py",
            "inputs": ["zones"],
        }

    _edit(meta, edit)
    with pytest.raises(ValueError, match=message.replace(".", r"\.")):
        load_meta_config(meta, verify_data=False)


def test_analysis_gpkg_input_is_accepted(tmp_path: Path) -> None:
    meta = _with_analysis(tmp_path)
    _edit(meta, lambda document: document["outputs"].update(zones={"file": "zones.gpkg"}))
    _edit(meta, lambda document: document["analyses"]["commute"].update(inputs=["zones"]))

    assert load_meta_config(meta, verify_data=False).analyses["commute"].inputs == (
        "zones",
    )


def test_analysis_script_must_be_a_regular_py_file(tmp_path: Path) -> None:
    meta = _with_analysis(tmp_path)
    folder = tmp_path / "analyses"
    (folder / "directory.py").mkdir()
    (folder / "notes.txt").write_text("x\n")
    make_symlink(folder / "link.py", folder / "commute.py")
    cases = {
        "analyses/missing.py": "analysis 'commute' script 'analyses/missing.py' does not exist",
        "analyses/directory.py": "analysis 'commute' script must be a regular file",
        "analyses/link.py": "analysis 'commute' script must not be a symlink",
        "analyses/notes.txt": r"analysis 'commute' script must be a \.py file",
    }
    for script, message in cases.items():
        _edit(meta, lambda document: document["analyses"]["commute"].update(script=script))
        with pytest.raises(ValueError, match=message):
            load_meta_config(meta, verify_data=False)
    absolute = str(folder / "commute.py")
    _edit(meta, lambda document: document["analyses"]["commute"].update(script=absolute))
    assert load_meta_config(meta, verify_data=False).analyses["commute"].script == (
        folder / "commute.py"
    ).resolve()


def test_interpreter_defaults_from_invocation(tmp_path: Path) -> None:
    meta = _with_analysis(tmp_path)

    def default_invocation(document: dict) -> None:
        del document["execution"]["python"]
        del document["execution"]["invocation"]

    _edit(meta, default_invocation)
    assert load_meta_config(meta, verify_data=False).analysis_python == (
        "uv", "run", "--locked", "python",
    )
    _edit(
        meta,
        lambda document: document["execution"].update(
            invocation=["pixi", "run", "python", "-m", "synpp"]
        ),
    )
    assert load_meta_config(meta, verify_data=False).analysis_python == (
        "pixi", "run", "python",
    )
    _edit(meta, lambda document: document.pop("execution"))
    assert load_meta_config(meta, verify_data=False).analysis_python == (
        "uv", "run", "--locked", "python",
    )


def test_interpreter_requires_execution_python_for_other_invocations(
    tmp_path: Path,
) -> None:
    meta = _with_analysis(tmp_path)
    _edit(meta, lambda document: document["execution"].pop("python"))

    with pytest.raises(ValueError) as error:
        load_meta_config(meta, verify_data=False)
    assert str(error.value) == NO_SYNPP
    # Without analyses, no interpreter is needed.
    _edit(meta, lambda document: document.pop("analyses"))
    assert load_meta_config(meta, verify_data=False).analyses == {}
    for python in ([], [""], "python"):
        _edit(meta, lambda document: document["execution"].update(python=python))
        with pytest.raises(ValueError, match="execution.python must be a non-empty list"):
            load_meta_config(meta, verify_data=False)


def test_value_indicator_on_analysis_output_loads(tmp_path: Path) -> None:
    meta = _with_analysis(
        tmp_path,
        {
            "script": "analyses/commute.py",
            "inputs": ["trips"],
            "category": "mode",
            "simulated": "share",
        },
    )
    _edit(
        meta,
        lambda document: document["indicators"].extend(
            [
                {"value": {"output": "commute", "column": "km", "where": {"mode": "car"}}},
                {"share": {"output": "commute", "category": "car"}},
            ]
        ),
    )

    loaded = load_meta_config(meta, verify_data=False)

    value, share = loaded.indicators[1:]
    derived = loaded.analyses["commute"].output
    assert value == ValueIndicator("commute", derived, "km", {"mode": "car"})
    assert share == ShareIndicator(
        TableArtifact("commute", derived, "mode", "share"), "car"
    )
    assert "commute" not in loaded.outputs
    assert analysis_identity(value)["artifact"] == {
        "name": "commute",
        "analysis": "commute",
        "inputs": ["trips"],
        "separator": ",",
        "column": "km",
        "where": {"mode": "car"},
    }
    _edit(
        meta,
        lambda document: document["indicators"].append(
            {"value": {"output": "nowhere", "column": "km"}}
        ),
    )
    with pytest.raises(ValueError, match="is not declared in outputs or analyses"):
        load_meta_config(meta, verify_data=False)


def test_primary_indicator_on_analysis_output_is_rejected(tmp_path: Path) -> None:
    meta = _with_analysis(
        tmp_path,
        {
            "script": "analyses/commute.py",
            "inputs": ["trips"],
            "category": "class",
            "simulated": "synthetic",
            "references": ["observed"],
        },
        method="canonical_morris",
    )

    def edit(document: dict) -> None:
        document["indicators"].append(
            {"tv_distance": {"output": "commute", "reference": "observed"}}
        )
        document["charter"]["decision"]["primary_indicator"] = (
            "tv_distance(commute, ref=observed)"
        )

    _edit(meta, edit)
    with pytest.raises(ValueError) as error:
        load_meta_config(meta, verify_data=False)
    assert str(error.value) == "primary_indicator must not use analysis output 'commute'"
    _edit(
        meta,
        lambda document: document["charter"]["decision"].update(
            primary_indicator=f"tv_distance(age, ref={REFERENCE})"
        ),
    )
    names = [item.name for item in load_meta_config(meta, verify_data=False).indicators]
    assert "tv_distance(commute, ref=observed)" in names


def test_default_indicators_ignore_analysis_outputs(tmp_path: Path) -> None:
    meta = _with_analysis(
        tmp_path,
        {
            "script": "analyses/commute.py",
            "inputs": ["trips"],
            "category": "class",
            "simulated": "synthetic",
            "references": ["observed"],
        },
    )
    _edit(meta, lambda document: document.pop("indicators"))

    names = [item.name for item in load_meta_config(meta, verify_data=False).indicators]

    assert names == [f"tv_distance(age, ref={REFERENCE})"]


@pytest.fixture
def spy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_CASE", "complete-references")


def test_adding_analyses_does_not_change_plan(tmp_path: Path, spy: None) -> None:
    meta, _ = campaign(tmp_path)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    plan = tmp_path / "campaign" / "plan.parquet"
    plan_bytes = plan.read_bytes()
    _script(tmp_path)

    def edit(document: dict) -> None:
        document["analyses"] = {
            "commute": {"script": "analyses/commute.py", "inputs": ["age"]}
        }
        document["execution"]["python"] = [sys.executable]

    _edit(meta, edit)

    assert plan_from_meta_config(meta).status == "unchanged"
    assert plan.read_bytes() == plan_bytes
    loaded = load_meta_config(meta, verify_data=False, use_sealed_plan=True)
    assert set(loaded.analyses) == {"commute"}
    assert set(loaded.outputs) == {"age"}
    assert analyze_from_meta_config(meta)["status"] == "completed"
    assert plan.read_bytes() == plan_bytes


def test_indicator_definition_v4_for_analysis_output() -> None:
    assert INDICATOR_DEFINITION_DOMAIN == b"eqasim-sa:indicator-definition:v4\0"
    derived = DerivedOutput(
        "commute", "commute", ("trips", "persons"), ",", "class", "synthetic", ("ref",)
    )
    from eqasim_sa.core.indicators import TotalVariationIndicator

    definition = analysis_identity(
        TotalVariationIndicator(TableArtifact("commute", derived, "class", "synthetic", ("ref",)), "ref")
    )
    assert definition["artifact"] == {
        "name": "commute",
        "analysis": "commute",
        "inputs": ["persons", "trips"],
        "separator": ",",
        "classColumn": "class",
        "syntheticColumn": "synthetic",
        "referenceColumns": ["ref"],
    }
    assert len(indicator_definition_sha256(definition)) == 64
    declared = analysis_identity(
        ValueIndicator("trips", OutputDeclaration("trips.csv", ";"), "km")
    )
    assert set(declared["artifact"]) == {"name", "file", "separator", "column", "where"}
    for change in (
        lambda artifact: artifact.update(file="x.csv"),
        lambda artifact: artifact.update(inputs=[]),
        lambda artifact: artifact.update(inputs=["trips", "persons"]),
        lambda artifact: artifact.update(inputs=["commute"]),
        lambda artifact: artifact.update(analysis="bad name"),
        lambda artifact: artifact.pop("analysis"),
    ):
        broken = analysis_identity(
            TotalVariationIndicator(TableArtifact("commute", derived, "class", "synthetic", ("ref",)), "ref")
        )
        change(broken["artifact"])
        with pytest.raises(ValueError, match="indicator definition"):
            validate_indicator_definition(broken)


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "bad name"},
        {"analysis": ""},
        {"inputs": ()},
        {"inputs": ("trips", "trips")},
        {"inputs": ("commute",)},
        {"separator": ";;"},
        {"category": ""},
    ],
)
def test_derived_output_rejects_malformed_fields(fields: dict) -> None:
    values = {
        "name": "commute",
        "analysis": "commute",
        "inputs": ("trips",),
        "separator": ",",
        "category": None,
    }
    values.update(fields)
    with pytest.raises(ValueError):
        DerivedOutput(**values)


def test_value_indicator_reads_an_analysis_output(tmp_path: Path) -> None:
    path = tmp_path / "commute.csv"
    path.write_text("mode,km\ncar,12.5\nbike,3\n")
    indicator = ValueIndicator(
        "commute", DerivedOutput("commute", "commute", ("trips",)), "km", {"mode": "car"}
    )

    assert indicator.compute(ArtifactSet({"commute": path})) == 12.5


def test_init_accepts_analyses_cache_and_rejects_foreign_content(tmp_path: Path) -> None:
    cache = tmp_path / "analyses-cache"
    entry = cache / "commute"
    entry.mkdir(parents=True)
    fingerprint = "a" * 64
    (entry / f"{fingerprint}.csv").write_text("x\n1\n")
    (entry / f"{fingerprint}.sha256").write_text("b" * 64 + "\n")
    (entry / f".{fingerprint}.csv.k3y.tmp").write_text("partial")
    (cache / "logs").mkdir()
    (cache / "logs" / f"commute-{'c' * 32}.log").write_text("Traceback\n")

    project_init_module._analyses_cache_allowed(cache, "grid")

    for foreign in (
        entry / "notes.txt",
        cache / "logs" / "readme.md",
        cache / "bad name",
    ):
        foreign.write_text("x")
        with pytest.raises(FileExistsError, match="foreign content"):
            project_init_module._analyses_cache_allowed(cache, "grid")
        os.remove(foreign)
    make_symlink(cache / "linked", entry, target_is_directory=True)
    with pytest.raises(FileExistsError, match="foreign content"):
        project_init_module._analyses_cache_allowed(cache, "grid")
