import copy
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from eqasim_sa.core.indicators import OutputDeclaration
from eqasim_sa.adapters.eqasim.config import EqasimSettings, load_config, load_yaml_document
from eqasim_sa.adapters.eqasim import execution as eqasim_execution
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator
from eqasim_sa.adapters import checks as check_service
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.adapters import application
from eqasim_sa.adapters.application import analyze_from_meta_config, plan_from_meta_config
from eqasim_sa.adapters.worker import Indexed, WorkerBatch
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.methods.multiscramble_sobol import _multiscramble_sobol_tables
from eqasim_sa.core.ledger import RunRecord
from eqasim_sa.core.plan import DiscreteOATDesign, GridDesign, SobolDesign, build_plan, load_run_specs
from eqasim_sa.core.run import RunResult, RunSpec, compute_run_id
from eqasim_sa.core.software_identity import read_software_identity
from eqasim_sa.ports.simulator import SimulationError
from tests.test_multiscramble_sobol_plan import multiscramble_meta_document
from tests.test_multiscramble_sobol_plan import multiscramble_design
from tests.test_plan import meta_fixture
from tests.provenance_helpers import write_csv
from tests.provenance_helpers import scientific_indicator, write_verified_run


CONFIGURATIONS = {
    "default": [
        "sex",
        "any_cars",
        "age_class",
        "socioprofessional_class",
        "departement_id",
    ],
    "reduced": ["sex", "age_class", "socioprofessional_class"],
    "age_priority": [
        "age_class",
        "any_cars",
        "sex",
        "socioprofessional_class",
        "departement_id",
    ],
}


def _tutorial_replacement(path: Path) -> dict:
    match = re.search(
        r"```yaml\n(# structured-grid-tutorial-replacement\n.*?)\n```",
        path.read_text(),
        re.DOTALL,
    )
    assert match is not None
    return yaml.safe_load(match.group(1))


def structured_meta(tmp_path: Path) -> Path:
    meta, model, config, _ = meta_fixture(tmp_path)
    config.write_text(
        config.read_text()
        + "  matching_attributes: [sex, any_cars, age_class, socioprofessional_class, departement_id]\n"
    )
    document = yaml.safe_load(meta.read_text())
    document["parameters"] = {
        "matching_attributes": {"configurations": copy.deepcopy(CONFIGURATIONS)}
    }
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    return meta


def test_load_meta_config_accepts_exact_named_ordered_list_syntax(tmp_path: Path) -> None:
    loaded = load_meta_config(structured_meta(tmp_path))

    assert loaded.factors == {
        "matching_attributes": ["default", "reduced", "age_priority"]
    }
    assert loaded.structured_factors == {
        "matching_attributes": CONFIGURATIONS
    }


def test_bilingual_tutorial_replacement_builds_six_grid_runs(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    replacement = _tutorial_replacement(
        root / "docs" / "structured-configurations-tutorial.md"
    )
    assert replacement == _tutorial_replacement(
        root / "docs-fr" / "structured-configurations-tutorial.md"
    )
    assert replacement["parameters"]["matching_attributes"]["configurations"] == CONFIGURATIONS

    meta = structured_meta(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document.update(replacement)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    loaded = load_meta_config(meta)
    assert loaded.seeds == (41, 42)
    result = plan_from_meta_config(meta)
    assert result.design_positions == 6
    assert result.unique_runs == 6


@pytest.mark.parametrize(
    ("configurations", "message"),
    [
        ({}, "must not be empty"),
        ({"": ["sex"]}, "labels"),
        ({"bad\0label": ["sex"]}, "labels"),
        ({"default": []}, "non-empty ordered list"),
        ({"default": ["sex", "sex"]}, "unique"),
        ({"default": [""]}, "non-empty"),
        ({"default": ["bad\0value"]}, "NUL"),
        ({"default": ["sex"], "alias": ["sex"]}, "distinct payloads"),
    ],
)
def test_load_meta_config_rejects_malformed_structured_configurations(
    tmp_path: Path, configurations: dict, message: str
) -> None:
    meta = structured_meta(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document["parameters"]["matching_attributes"]["configurations"] = configurations
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    with pytest.raises((TypeError, ValueError), match=message):
        load_meta_config(meta)


@pytest.mark.parametrize("base_value", ["sex", ["sex", 2]])
def test_structured_factor_requires_existing_string_list_model_key(
    tmp_path: Path, base_value: object
) -> None:
    meta = structured_meta(tmp_path)
    model = tmp_path / "model"
    document = yaml.safe_load((model / "config.yml").read_text())
    document["config"]["matching_attributes"] = base_value
    (model / "config.yml").write_text(yaml.safe_dump(document, sort_keys=False))

    with pytest.raises(TypeError, match="model config value.*list of strings"):
        load_meta_config(meta)


def test_structured_factor_requires_existing_model_key(tmp_path: Path) -> None:
    meta = structured_meta(tmp_path)
    model = tmp_path / "model"
    document = yaml.safe_load((model / "config.yml").read_text())
    del document["config"]["matching_attributes"]
    (model / "config.yml").write_text(yaml.safe_dump(document, sort_keys=False))

    with pytest.raises(ValueError, match="unknown factor"):
        load_meta_config(meta)


def test_scientific_structured_factor_applies_distribution_to_labels(
    tmp_path: Path,
) -> None:
    meta = structured_meta(tmp_path)
    document = multiscramble_meta_document(meta)
    document["experiment"]["scramble_seeds"] = [1, 3, 5, 10]
    distribution = {
        "family": "discrete_uniform",
        "transform": "identity",
        "provenance": "named matching configurations",
    }
    document["parameters"]["matching_attributes"] = {
        "configurations": CONFIGURATIONS,
        "distribution": distribution,
    }
    document["charter"]["uncertainty"]["inventory"]["parameters"] = [
        {
            "name": "matching_attributes",
            "status": "varied",
            "provenance": "campaign",
            "rationale": "named model structure",
        },
        {
            "name": "seed",
            "status": "varied",
            "provenance": "campaign",
            "rationale": "finite simulator seed factor",
        },
    ]
    document["charter"]["budget"] = {"maximum_runs": 300, "reserve_fraction": 0.0}
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    loaded = load_meta_config(meta)
    assert loaded.factors["matching_attributes"] == [
        "default",
        "reduced",
        "age_priority",
    ]
    assert loaded.distributions["matching_attributes"].family == "discrete_uniform"
    plan_from_meta_config(meta)
    path = loaded.campaign_root / "plan.parquet"
    table = pq.read_table(path)
    assert table.schema.metadata[b"planSchemaVersion"] == b"3"
    metadata = dict(table.schema.metadata or {})
    changed = copy.deepcopy(CONFIGURATIONS)
    changed["reduced"] = ["other"]
    metadata[b"structuredFactors"] = json.dumps(
        {"matching_attributes": changed}, sort_keys=True, separators=(",", ":")
    ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), path)
    with pytest.raises(ValueError, match="mismatch"):
        analyze_from_meta_config(meta)


def test_trajectory_methods_refuse_structured_factors(tmp_path: Path) -> None:
    grid = _grid_design(tmp_path / "unused")
    for method in ("discrete_oat", "canonical_morris"):
        design = DiscreteOATDesign(
            grid.base_config,
            grid.factors,
            grid.seeds,
            grid.context_hash,
            grid.context_descriptor,
            tmp_path / method,
            1,
            1,
            method=method,
            structured_factors=grid.structured_factors,
        )
        with pytest.raises(ValueError, match="does not support structured factors"):
            build_plan(design)


def _grid_design(root: Path, *, structured_factors=None) -> GridDesign:  # type: ignore[no-untyped-def]
    descriptor = {
        "eqasimSourceSha256": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.3.0",
    }
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    return GridDesign(
        base_config={
            "working_directory": "/ignored",
            "run": ["synthesis.output", "analysis.synthesis.population"],
            "config": {
                "matching_attributes": CONFIGURATIONS["default"],
                "random_seed": 9,
            },
        },
        factors={"matching_attributes": ["default", "reduced", "age_priority"]},
        seeds=(41,),
        context_hash=hashlib.sha256(
            b"eqasim-sa:context:v3\0" + encoded
        ).hexdigest(),
        context_descriptor=descriptor,
        campaign_root=root,
        structured_factors=structured_factors or {
            "matching_attributes": copy.deepcopy(CONFIGURATIONS)
        },
    )


def test_plan_v3_stores_labels_but_run_specs_resolve_lists_and_hash_them(
    tmp_path: Path,
) -> None:
    design = _grid_design(tmp_path / "campaign")
    build_plan(design)
    path = design.campaign_root / "plan.parquet"
    table = pq.read_table(path)

    assert table["matching_attributes"].to_pylist() == [
        "default",
        "reduced",
        "age_priority",
    ]
    assert table.schema.metadata[b"planSchemaVersion"] == b"3"
    assert table.schema.metadata[b"structuredFactorsVersion"] == b"1"
    assert table.schema.metadata[b"structuredFactors"] == json.dumps(
        {"matching_attributes": CONFIGURATIONS},
        separators=(",", ":"),
    ).encode()

    specs = load_run_specs(path, design, "demo")
    assert [spec.parameters["matching_attributes"] for spec in specs] == [
        "default",
        "reduced",
        "age_priority",
    ]
    assert [spec.resolved_config["config"]["matching_attributes"] for spec in specs] == list(
        CONFIGURATIONS.values()
    )
    assert [row["runId"] for row in table.to_pylist()] == [
        compute_run_id(spec.resolved_config, spec.seed) for spec in specs
    ]


@pytest.mark.parametrize("tamper", ["mapping", "version", "missing"])
def test_structured_plan_metadata_pair_is_exact_and_tamper_evident(
    tmp_path: Path, tamper: str
) -> None:
    design = _grid_design(tmp_path / "campaign")
    build_plan(design)
    path = design.campaign_root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    if tamper == "mapping":
        changed = copy.deepcopy(design.structured_factors)
        changed["matching_attributes"]["reduced"] = ["other"]
        metadata[b"structuredFactors"] = json.dumps(
            changed, sort_keys=True, separators=(",", ":")
        ).encode()
    elif tamper == "version":
        metadata[b"structuredFactorsVersion"] = b"2"
    else:
        del metadata[b"structuredFactors"]
    pq.write_table(table.replace_schema_metadata(metadata), path)

    with pytest.raises(ValueError, match="mismatch"):
        load_run_specs(path, design, "demo")


def test_grid_analysis_rejects_noncanonical_structured_metadata(tmp_path: Path) -> None:
    design = _grid_design(tmp_path / "campaign")
    build_plan(design)
    path = design.campaign_root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    metadata[b"structuredFactors"] += b" "
    pq.write_table(table.replace_schema_metadata(metadata), path)

    with pytest.raises(ValueError, match="structured.*metadata"):
        analyze(
            design.campaign_root,
            (),
            campaign="demo",
            completeness_threshold=0.0,
        )


def _tamper_payload_metadata(
    table: pa.Table, method_key: bytes | None = None
) -> pa.Table:
    metadata = dict(table.schema.metadata or {})
    mapping = json.loads(metadata[b"structuredFactors"])
    factor = next(iter(mapping))
    label = next(iter(mapping[factor]))
    mapping[factor][label] = ["payload-only-tamper"]
    metadata[b"structuredFactors"] = json.dumps(
        mapping, separators=(",", ":")
    ).encode()
    if method_key is not None:
        method = json.loads(metadata[method_key])
        method["structuredFactors"] = mapping
        metadata[method_key] = json.dumps(
            method, sort_keys=True, separators=(",", ":")
        ).encode()
    return table.replace_schema_metadata(metadata)


def test_grid_cli_preflight_rejects_payload_only_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = structured_meta(tmp_path)
    plan_from_meta_config(meta)
    loaded = load_meta_config(meta)
    path = loaded.campaign_root / "plan.parquet"
    pq.write_table(_tamper_payload_metadata(pq.read_table(path)), path)
    called = []
    monkeypatch.setattr(application, "analyze", lambda *args, **kwargs: called.append(1))

    with pytest.raises(ValueError, match="mismatch"):
        analyze_from_meta_config(meta)
    assert called == []


@pytest.mark.parametrize(
    "tamper", ["unrelated", "missing", "extra", "reordered", "observed_unknown"]
)
def test_grid_analysis_rejects_structured_factor_semantic_tampering(
    tmp_path: Path, tamper: str
) -> None:
    design = _grid_design(tmp_path / "campaign")
    build_plan(design)
    path = design.campaign_root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    mapping = copy.deepcopy(design.structured_factors)
    if tamper == "unrelated":
        mapping["unrelated"] = {"only": ["x"]}
    elif tamper == "missing":
        del mapping["matching_attributes"]["age_priority"]
    elif tamper == "extra":
        mapping["matching_attributes"]["extra"] = ["other"]
    elif tamper == "reordered":
        values = mapping["matching_attributes"]
        mapping["matching_attributes"] = {
            name: values[name]
            for name in ("age_priority", "default", "reduced")
        }
    else:
        index = table.schema.get_field_index("matching_attributes")
        table = table.set_column(
            index,
            table.schema.field(index),
            pa.array(["unknown", "reduced", "age_priority"]),
        )
    metadata[b"structuredFactors"] = json.dumps(
        {name: mapping[name] for name in sorted(mapping)},
        separators=(",", ":"),
    ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), path)

    with pytest.raises(ValueError, match="structured"):
        analyze(
            design.campaign_root,
            (),
            campaign="demo",
            completeness_threshold=0.0,
        )


def _structured_sobol_campaign(root: Path) -> Path:
    descriptor = {
        "eqasimSourceSha256": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.3.0",
    }
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    support = ["default", "reduced", "age_priority"]
    design = SobolDesign(
        {"config": {"matching_attributes": CONFIGURATIONS["default"]}},
        {"matching_attributes": support},
        (11, 12, 13),
        hashlib.sha256(b"eqasim-sa:context:v3\0" + encoded).hexdigest(),
        descriptor,
        root,
        base_sample_size=8,
        design_seed=20260814,
        structured_factors={"matching_attributes": copy.deepcopy(CONFIGURATIONS)},
    )
    build_plan(design)
    table = pq.read_table(root / "plan.parquet")
    for row in {row["runId"]: row for row in table.to_pylist()}.values():
        value = float(support.index(row["matching_attributes"]) + row["seed"] / 10)
        write_csv(
            pa.table({"synthetic": [value]}),
            root / "archives" / row["runId"] / "age.csv",
        )
        write_verified_run(
            root,
            RunRecord(
                row["runId"],
                "demo",
                row["seed"],
                design.context_hash,
                "node",
                1.0,
                {"matching_attributes": row["matching_attributes"]},
            ),
        )
    return root


def test_structured_sobol_cli_preflight_rejects_payload_only_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = structured_meta(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document["experiment"] = {
        "method": "sobol",
        "seeds": [101, 102, 103],
        "base_sample_size": 4,
        "design_seed": 20260814,
    }
    document["analysis"] = {
        "completeness_threshold": 1.0,
        "bootstrap_resamples": 19,
        "confidence_level": 0.95,
    }
    document["outputs"] = {
        "age": {"category": "c", "simulated": "s", "references": ["ref"]}
    }
    document["indicators"] = [{"tv_distance": {"output": "age", "reference": "ref"}}]
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    plan_from_meta_config(meta)
    loaded = load_meta_config(meta)
    path = loaded.campaign_root / "plan.parquet"
    pq.write_table(_tamper_payload_metadata(pq.read_table(path)), path)
    called = []
    monkeypatch.setattr(application, "analyze", lambda *args, **kwargs: called.append(1))

    with pytest.raises(ValueError, match="mismatch"):
        analyze_from_meta_config(meta)
    assert called == []


@pytest.mark.parametrize("tamper", ["unrelated", "missing", "extra", "reordered"])
def test_structured_sobol_analysis_rejects_structured_support_tampering(
    tmp_path: Path, tamper: str
) -> None:
    root = _structured_sobol_campaign(tmp_path / "sobol")
    path = root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    mapping = json.loads(metadata[b"structuredFactors"])
    values = mapping["matching_attributes"]
    if tamper == "unrelated":
        mapping = {"unrelated": values}
    elif tamper == "missing":
        del values["age_priority"]
    elif tamper == "extra":
        values["extra"] = ["other"]
    else:
        mapping["matching_attributes"] = {
            name: values[name]
            for name in ("age_priority", "default", "reduced")
        }
    metadata[b"structuredFactors"] = json.dumps(
        {name: mapping[name] for name in sorted(mapping)}, separators=(",", ":")
    ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), path)

    with pytest.raises(ValueError, match="structured"):
        analyze(
            root,
            (scientific_indicator("age"),),
            campaign="demo",
            completeness_threshold=1.0,
            seed_role="factor",
            method="sobol",
            bootstrap_resamples=10,
            confidence_level=0.95,
        )


@pytest.mark.parametrize("target", ["generic", "method", "both"])
def test_structured_sobol_direct_analysis_rejects_payload_only_tampering(
    tmp_path: Path, target: str
) -> None:
    root = _structured_sobol_campaign(tmp_path / "sobol")
    path = root / "plan.parquet"
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    generic = json.loads(metadata[b"structuredFactors"])
    method = json.loads(metadata[b"sobolDesign"])
    changed = copy.deepcopy(generic)
    changed["matching_attributes"]["reduced"] = ["payload-only-tamper"]
    if target in {"generic", "both"}:
        metadata[b"structuredFactors"] = json.dumps(
            changed, separators=(",", ":")
        ).encode()
    if target in {"method", "both"}:
        method["structuredFactors"] = changed
        metadata[b"sobolDesign"] = json.dumps(
            method, sort_keys=True, separators=(",", ":")
        ).encode()
    pq.write_table(table.replace_schema_metadata(metadata), path)

    with pytest.raises(ValueError, match="Sobol|structured|designId"):
        analyze(
            root,
            (scientific_indicator("age"),),
            campaign="demo",
            completeness_threshold=1.0,
            seed_role="factor",
            method="sobol",
            bootstrap_resamples=10,
            confidence_level=0.95,
        )


def _structured_multiscramble_design(root: Path):  # type: ignore[no-untyped-def]
    original = multiscramble_design(root)
    mapping = {"a": {"low": ["x"], "high": ["x", "y"]}}
    base_config = copy.deepcopy(original.base_config)
    base_config["config"]["a"] = ["x"]
    contract = copy.deepcopy(original.scientific_contract)
    for parameter in contract["parameters"]:
        if parameter["name"] == "a":
            parameter["support"] = ["low", "high"]
    return replace(
        original,
        base_config=base_config,
        factors={"a": ["low", "high"]},
        scientific_contract=contract,
        structured_factors=mapping,
    )


@pytest.mark.parametrize("target", ["generic", "method", "both"])
def test_multiscramble_direct_analysis_rejects_payload_only_tampering(
    tmp_path: Path, target: str
) -> None:
    design = _structured_multiscramble_design(tmp_path / "multiscramble")
    build_plan(design)
    table = pq.read_table(design.campaign_root / "plan.parquet")
    metadata = dict(table.schema.metadata or {})
    generic = json.loads(metadata[b"structuredFactors"])
    method = json.loads(metadata[b"multiScrambleSobolDesign"])
    assert method["structuredFactors"] == generic
    changed = copy.deepcopy(generic)
    changed["a"]["low"] = ["payload-only-tamper"]
    if target in {"generic", "both"}:
        metadata[b"structuredFactors"] = json.dumps(
            changed, separators=(",", ":")
        ).encode()
    if target in {"method", "both"}:
        method["structuredFactors"] = changed
        metadata[b"multiScrambleSobolDesign"] = json.dumps(
            method, sort_keys=True, separators=(",", ":")
        ).encode()
    damaged = table.replace_schema_metadata(metadata)

    with pytest.raises(ValueError, match="multi-scramble Sobol|structured|designId"):
        _multiscramble_sobol_tables(damaged, [], design.confidence_level)


def test_unstructured_plan_keeps_v1_schema_and_exact_bytes(tmp_path: Path) -> None:
    from tests.test_plan import design as existing_grid_design

    baseline = existing_grid_design(tmp_path / "baseline")
    replay = existing_grid_design(tmp_path / "replay")
    build_plan(baseline)
    build_plan(replay)
    assert pq.read_table(baseline.campaign_root / "plan.parquet").schema.metadata[
        b"planSchemaVersion"
    ] == b"1"
    assert (baseline.campaign_root / "plan.parquet").read_bytes() == (
        replay.campaign_root / "plan.parquet"
    ).read_bytes()


def _simulator(tmp_path: Path) -> tuple[EqasimSimulator, dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.yml"
    config.write_text(
        "working_directory: /ignored\n"
        "run: [synthesis.output, analysis.synthesis.population]\n"
        "config:\n"
        "  random_seed: 1\n"
        "  output_prefix: nantes_\n"
        "  output_path: /ignored\n"
        "  matching_attributes: [sex, any_cars]\n"
        "  sampling_rate: 0.001\n"
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    settings = EqasimSettings(
        config,
        tmp_path,
        ("ignored",),
        "test",
        scratch,
        "c" * 64,
    )
    base = load_config(config, 42)
    simulator = EqasimSimulator(
        settings,
        base,
        structured_factors={
            "matching_attributes": {
                "default": ["sex", "any_cars"],
                "reduced": ["sex"],
            }
        },
        outputs={"persons": OutputDeclaration("{prefix}persons.csv", ";")},
    )
    return simulator, base


def test_simulator_writes_structured_snapshot_and_omits_list_cli_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulator, resolved = _simulator(tmp_path)
    resolved["config"]["matching_attributes"] = ["sex"]
    observed = {}

    def run(command, **options):  # type: ignore[no-untyped-def]
        observed["command"] = command
        return type("Completed", (), {"returncode": 7})()

    monkeypatch.setattr(simulator, "_run_owned", run)
    with pytest.raises(SimulationError, match="code 7") as failure:
        simulator.simulate(resolved, 42, simulator.workspace.root)

    snapshot = load_yaml_document(failure.value.attempt_directory / "config.yml")
    assert snapshot["config"]["matching_attributes"] == ["sex"]
    assert "matching_attributes" not in observed["command"]


@pytest.mark.parametrize("undeclared", [["x"], {"x": 1}])
def test_simulator_rejects_undeclared_composite_change(
    tmp_path: Path, undeclared: object
) -> None:
    simulator, resolved = _simulator(tmp_path)
    resolved["config"]["undeclared"] = undeclared
    with pytest.raises(TypeError, match="undeclared"):
        simulator.simulate(resolved, 42, simulator.workspace.root)


def test_simulator_keeps_scalar_cli_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    simulator, resolved = _simulator(tmp_path / "scalar")
    resolved["config"]["sampling_rate"] = 0.25
    observed = {}

    def run(command, **options):  # type: ignore[no-untyped-def]
        observed["command"] = command
        return type("Completed", (), {"returncode": 7})()

    monkeypatch.setattr(simulator, "_run_owned", run)
    with pytest.raises(SimulationError):
        simulator.simulate(resolved, 42, simulator.workspace.root)
    index = observed["command"].index("sampling_rate")
    assert observed["command"][index - 1] == "--config"
    assert observed["command"][index + 1] == "0.25"
    assert (
        next(simulator.workspace.root.glob("a*/config.yml"))
    ).read_bytes() == simulator.config_bytes


def _failed_execution_spy(observed: list[tuple[list[str], dict]]):  # type: ignore[no-untyped-def]
    def execute(spec, simulator, campaign_root, workdir, interrupted):  # type: ignore[no-untyped-def]
        try:
            simulator.simulate(spec.resolved_config, spec.seed, workdir)
        except SimulationError as error:
            observed.append(
                (
                    simulator._observed_command,
                    load_yaml_document(error.attempt_directory / "config.yml"),
                )
            )
            return RunResult(compute_run_id(spec.resolved_config, spec.seed), "failed", str(error))
        raise AssertionError("model launch spy must fail")

    return execute


@pytest.mark.posix_only
def test_slurm_callable_preserves_structured_snapshot_without_list_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eqasim_sa.adapters.backends import slurm

    real_simulator = EqasimSimulator
    fixture, base = _simulator(tmp_path / "model")
    fixture.interrupt()
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    software_identity = read_software_identity()
    descriptor = {
        "eqasimSourceSha256": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.3.0",
    }
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    context_hash = hashlib.sha256(b"eqasim-sa:context:v3\0" + encoded).hexdigest()
    meta_path = tmp_path / "meta.yml"
    meta_path.write_bytes(b"sealed meta\n")
    mapping = {
        "matching_attributes": {
            "default": ["sex", "any_cars"],
            "reduced": ["sex"],
        }
    }
    meta = SimpleNamespace(
        context_descriptor=descriptor,
        context_hash=context_hash,
        execution=SimpleNamespace(
            backend="slurm",
            invocation=("ignored",),
            scratch=tmp_path / "node-scratch",
        ),
        model_config=tmp_path / "model" / "config.yml",
        model_root=tmp_path / "model",
        campaign="demo",
        model_document=base,
        model_bytes=(tmp_path / "model" / "config.yml").read_bytes(),
        campaign_root=campaign,
        structured_factors=mapping,
        outputs={"persons": OutputDeclaration("{prefix}persons.csv", ";")},
        software_identity=software_identity,
        eqasim_sa_sha256=software_identity.sha256,
    )
    meta.execution.scratch.mkdir()
    monkeypatch.setattr(slurm, "load_meta_config", lambda path: meta)
    observed: list[tuple[list[str], dict]] = []

    def construct(*args, **kwargs):  # type: ignore[no-untyped-def]
        simulator = real_simulator(*args, **kwargs)

        def launch(command, **options):  # type: ignore[no-untyped-def]
            simulator._observed_command = command
            return SimpleNamespace(returncode=7)

        simulator._run_owned = launch
        return simulator

    monkeypatch.setattr(eqasim_execution, "EqasimSimulator", construct)
    monkeypatch.setattr(
        eqasim_execution, "execute_run", _failed_execution_spy(observed)
    )
    resolved = copy.deepcopy(base)
    resolved["config"]["matching_attributes"] = ["sex"]
    resolved["config"]["random_seed"] = 41
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.enforce_same_machine", lambda *args: None
    )
    spec = RunSpec("demo", resolved, 41, context_hash, {"matching_attributes": "reduced"})
    digest = hashlib.sha256(meta_path.read_bytes()).hexdigest()

    result = slurm.EqasimSlurmJob(
        meta_path, context_hash, digest, software_identity.sha256
    )(
        WorkerBatch(0, (Indexed(0, spec),))
    )

    assert result[0].value.status == "failed"
    assert observed[0][1]["config"]["matching_attributes"] == ["sex"]
    assert "matching_attributes" not in observed[0][0]


def test_public_smoke_check_preserves_structured_snapshot_without_list_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_simulator = EqasimSimulator
    fixture, base = _simulator(tmp_path / "model")
    fixture.interrupt()
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    mapping = {
        "matching_attributes": {
            "default": ["sex", "any_cars"],
            "reduced": ["sex"],
        }
    }
    software_identity = read_software_identity()
    meta = SimpleNamespace(
        seeds=(41, 42),
        model_config=tmp_path / "model" / "config.yml",
        campaign_root=campaign,
        model_root=tmp_path / "model",
        execution=SimpleNamespace(invocation=("ignored",), scratch=scratch),
        model_document=base,
        model_bytes=(tmp_path / "model" / "config.yml").read_bytes(),
        structured_factors=mapping,
        outputs={"persons": OutputDeclaration("{prefix}persons.csv", ";")},
        software_identity=software_identity,
        context_hash="c" * 64,
    )
    resolved = copy.deepcopy(base)
    resolved["config"]["matching_attributes"] = ["sex"]
    spec = RunSpec("demo", resolved, 41, "c" * 64, {"matching_attributes": "reduced"})
    payload = {"status": "valid", "checks": []}
    monkeypatch.setattr(
        check_service,
        "_static_check",
        lambda path, calibrations: (payload, meta, (spec,)),
    )
    observed: list[tuple[list[str], dict]] = []

    def construct(*args, **kwargs):  # type: ignore[no-untyped-def]
        simulator = real_simulator(*args, **kwargs)

        def launch(command, **options):  # type: ignore[no-untyped-def]
            simulator._observed_command = command
            return SimpleNamespace(returncode=7)

        simulator._run_owned = launch
        return simulator

    monkeypatch.setattr(eqasim_execution, "EqasimSimulator", construct)
    monkeypatch.setattr(
        eqasim_execution, "execute_run", _failed_execution_spy(observed)
    )

    result = application.check_from_meta_config(Path("meta.yml"), (), True)

    assert result["status"] == "invalid"
    assert result["checks"][-1]["name"] == "smoke"
    assert len(observed) == 2
    assert all(
        snapshot["config"]["matching_attributes"] == ["sex"]
        for _, snapshot in observed
    )
    assert all("matching_attributes" not in command for command, _ in observed)
