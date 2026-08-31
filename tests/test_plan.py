import copy
import hashlib
import json
import math
import subprocess
import threading
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import eqasim_sa.core.plan as plan_module
from eqasim_sa.adapters.application import plan_from_meta_config
from eqasim_sa.adapters.eqasim.config import load_yaml_document
from eqasim_sa.adapters.eqasim.simulator import EqasimSimulator, eqasim_requirements
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.indicators import ArtifactSet
from eqasim_sa.core.plan import GridDesign, build_plan
from eqasim_sa.core.run import compute_run_id
from eqasim_sa.ports.simulator import IndicatorRequirement
from tests.provenance_helpers import (
    context_hash as provenance_context_hash,
    scientific_context_descriptor,
)


class _PluginDistribution:
    name = "test-indicator-plugin"
    version = "1.0.0"
    files = (Path("tests/test_plan.py"),)

    def locate_file(self, path: Path) -> Path:
        return Path(__file__).parent.parent / path


def _context() -> tuple[dict[str, str], str]:
    descriptor = scientific_context_descriptor()
    return descriptor, provenance_context_hash(descriptor)


def base() -> dict:
    return {
        "working_directory": "/ignored",
        "run": ["old"],
        "config": {"a": 0, "b": "x", "random_seed": 9},
    }


def design(root: Path, **changes) -> GridDesign:
    descriptor = {
        "eqasimCommit": "a",
        "dataManifestSha256": "b",
        "lockfileSha256": "c",
        "eqasimSaVersion": "0.1.0",
    }
    context_hash = hashlib.sha256(
        b"eqasim-sa:context:v1\0"
        + json.dumps(
            descriptor,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    values = dict(
        base_config=base(),
        factors={"b": ["x", "y", "z"], "a": [1, 2]},
        seeds=(41, 42),
        requirements={"run": ["stage"]},
        context_hash=context_hash,
        context_descriptor=descriptor,
        campaign_root=root,
    )
    values.update(changes)
    return GridDesign(**values)


def morris_design(root: Path, **changes):  # type: ignore[no-untyped-def]
    descriptor, context_hash = _context()
    values = dict(
        base_config=base(),
        factors={"b": [-1.0, 1.0], "a": [0, 2, 5]},
        seeds=(41, 42),
        requirements={"run": ["stage"]},
        context_hash=context_hash,
        context_descriptor=descriptor,
        campaign_root=root,
        trajectories=3,
        design_seed=20260814,
    )
    values.update(changes)
    return plan_module.MorrisDesign(**values)


def sobol_design(root: Path, **changes):  # type: ignore[no-untyped-def]
    descriptor, context_hash = _context()
    values = dict(
        base_config=base(),
        factors={"b": ["x", "y", "z"], "a": [1, 2]},
        seeds=(101, 102, 103),
        requirements={"run": ["stage"]},
        context_hash=context_hash,
        context_descriptor=descriptor,
        campaign_root=root,
        base_sample_size=4,
        design_seed=20260814,
    )
    values.update(changes)
    return plan_module.SobolDesign(**values)


def _sobol_meta(meta: Path, **changes: object) -> None:
    values = {
        "base_sample_size": 4,
        "design_seed": 20260814,
        "bootstrap_resamples": 19,
        "confidence_level": 0.95,
    }
    values.update(changes)
    text = meta.read_text().replace(
        "analysis:\n  completeness_threshold: 1.0\n",
        "analysis:\n  completeness_threshold: 1.0\n"
        f"  bootstrap_resamples: {values['bootstrap_resamples']}\n"
        f"  confidence_level: {values['confidence_level']}\n",
    )
    text = text.replace(
        "  method: grid\n  seed_role: replication\n  replications:\n    seeds: [41, 42]\n",
        "  method: sobol\n  seed_role: factor\n"
        f"  base_sample_size: {values['base_sample_size']}\n"
        f"  design_seed: {values['design_seed']}\n",
    )
    text = text.replace(
        "parameters:\n",
        "parameters:\n  seed:\n    values: [101, 102, 103]\n",
    )
    text += (
        "artifacts:\n  age: {class: c, synthetic: s, references: [ref]}\n"
        "indicators:\n  - pair: {scalarizer: tv_distance, artifact: age, reference: ref}\n"
    )
    meta.write_text(text)


def test_sobol_exact_qmc_mapping_roles_identity_metadata_and_cardinality(
    tmp_path: Path,
) -> None:
    from scipy.stats import qmc

    current = sobol_design(tmp_path / "campaign")
    result = build_plan(current)
    table = pq.read_table(current.campaign_root / "plan.parquet")
    rows = table.to_pylist()
    names = ("a", "b", "seed")
    domains = {"a": [1, 2], "b": ["x", "y", "z"], "seed": [101, 102, 103]}
    sample = qmc.Sobol(
        d=6, scramble=True, bits=64, rng=20260814, optimization=None
    ).random_base2(m=2)
    projected = [
        tuple(
            domains[name][math.floor(float(u) * len(domains[name]))]
            for name, u in zip(names * 2, point)
        )
        for point in sample
    ]
    expected = []
    definition = {
        "baseSampleSize": 4,
        "designSeed": 20260814,
        "scramble": True,
        "bits": 64,
        "mapping": "equal-width-v1",
        "factors": [{"name": name, "values": domains[name]} for name in names],
    }
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    for row_index in range(4):
        a, b = projected[row_index][:3], projected[row_index][3:]
        positions = [(f"A:row:{row_index}", a), (f"B:row:{row_index}", b)]
        for index, name in enumerate(names):
            ab, ba = list(a), list(b)
            ab[index], ba[index] = b[index], a[index]
            positions.extend(
                (
                    (f"AB:factor:{name}:row:{row_index}", tuple(ab)),
                    (f"BA:factor:{name}:row:{row_index}", tuple(ba)),
                )
            )
        for role, values in positions:
            design_id = hashlib.sha256(
                b"eqasim-sa:design:sobol:v1\0"
                + json.dumps(
                    {"design": definition, "role": role},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:32]
            expected.append((role, values, design_id))
    assert result.design_positions == 4 * (2 * 3 + 2) == len(rows)
    assert [
        (row["role"], tuple(row[name] for name in names), row["designId"])
        for row in rows
    ] == expected
    assert table.column_names == [*plan_module.FIXED, "a", "b"]
    assert table.schema.metadata == {
        b"planSchemaVersion": b"1",
        b"canonicalizationVersion": b"1",
        b"contextDescriptor": json.dumps(
            current.context_descriptor, sort_keys=True, separators=(",", ":")
        ).encode(),
        b"sobolDesignVersion": b"1",
        b"sobolDesign": encoded,
    }
    assert all(row["seed"] in domains["seed"] for row in rows)


def test_sobol_rejects_factor_without_effective_projected_contrast(tmp_path: Path):
    descriptor, context_hash = _context()
    design = plan_module.SobolDesign(
        base(),
        {"sampling_rate": [0.001, 0.002], "matching_age_scheme": ["matching_6", "analysis_7"]},
        (1401, 1402),
        {},
        context_hash,
        descriptor,
        tmp_path / "degenerate",
        2,
        20260814,
    )
    with pytest.raises(ValueError, match="no effective hybrid contrast"):
        build_plan(design)
    assert not (design.campaign_root / "plan.parquet").exists()


def test_sobol_accepts_identifiable_factors_with_position_deduplication(tmp_path: Path):
    descriptor, context_hash = _context()
    design = plan_module.SobolDesign(
        base(),
        {"sampling_rate": [0.001, 0.002], "matching_age_scheme": ["matching_6", "analysis_7"]},
        (1401, 1402),
        {},
        context_hash,
        descriptor,
        tmp_path / "valid",
        4,
        20260814,
    )
    result = build_plan(design)
    assert result.design_positions == 32
    assert result.unique_runs == 8 < result.design_positions


def test_sobol_is_deterministic_parameter_order_independent_and_sensitive(
    tmp_path: Path,
) -> None:
    baseline = sobol_design(tmp_path / "baseline")
    reordered = sobol_design(
        tmp_path / "reordered", factors={"a": [1, 2], "b": ["x", "y", "z"]}
    )
    changed_seed = sobol_design(tmp_path / "seed", design_seed=20260815)
    changed_domain = sobol_design(
        tmp_path / "domain", factors={"a": [2, 1], "b": ["x", "y", "z"]}
    )
    for current in (baseline, reordered, changed_seed, changed_domain):
        build_plan(current)
    original = (baseline.campaign_root / "plan.parquet").read_bytes()
    assert (reordered.campaign_root / "plan.parquet").read_bytes() == original
    assert (changed_seed.campaign_root / "plan.parquet").read_bytes() != original
    assert (changed_domain.campaign_root / "plan.parquet").read_bytes() != original
    assert build_plan(baseline).status == "unchanged"
    assert (baseline.campaign_root / "plan.parquet").read_bytes() == original


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"base_sample_size": 1}, "base_sample_size"),
        ({"base_sample_size": 3}, "power of two"),
        ({"base_sample_size": True}, "base_sample_size"),
        ({"base_sample_size": 2**64}, r"N\(2D\+2\).*int64"),
        ({"design_seed": -1}, "design_seed"),
        ({"design_seed": 2**64}, "design_seed"),
        ({"design_seed": True}, "design_seed"),
        ({"factors": {}}, "two factors"),
        ({"factors": {"a": [1]}}, "two.*values"),
        ({"factors": {"a": [1, 1]}}, "distinct"),
        ({"factors": {"a": [1, 2.0]}}, "same type"),
        ({"seeds": (1,)}, "two factors|two.*values"),
        ({"seeds": (1, 1)}, "distinct|unique"),
    ],
)
def test_sobol_core_rejects_invalid_contract(
    tmp_path: Path,
    changes: dict,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        build_plan(sobol_design(tmp_path / "campaign", **changes))


def test_sobol_rejects_int64_cardinality_before_qmc_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        plan_module.qmc,
        "Sobol",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("QMC allocated")),
    )
    with pytest.raises(ValueError, match=r"N\(2D\+2\).*int64"):
        build_plan(sobol_design(tmp_path / "campaign", base_sample_size=2**60))


def test_sobol_design_api_reserves_seed_and_requires_two_seed_values(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="SobolDesign factors must not contain.*seed"):
        build_plan(
            sobol_design(tmp_path / "reserved", factors={"a": [1, 2], "seed": [3, 4]})
        )
    with pytest.raises(ValueError, match="Sobol.*seed.*at least two"):
        build_plan(sobol_design(tmp_path / "single", seeds=(101,)))


def test_sobol_meta_config_preserves_seed_factor_and_analysis_contract(
    tmp_path: Path,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _sobol_meta(meta)
    loaded = load_meta_config(meta)
    assert loaded.method == "sobol" and loaded.seed_role == "factor"
    assert loaded.factors == {"a": [1, 2], "b": ["x", "y"]}
    assert loaded.seeds == (101, 102, 103)
    assert loaded.base_sample_size == 4 and loaded.design_seed == 20260814
    assert loaded.bootstrap_resamples == 19 and loaded.confidence_level == 0.95


@pytest.mark.parametrize(
    "change,message",
    [
        (
            lambda text: text.replace("  base_sample_size: 4\n", ""),
            "base_sample_size|keys",
        ),
        (
            lambda text: text.replace("  design_seed: 20260814\n", ""),
            "design_seed|keys",
        ),
        (
            lambda text: text.replace(
                "  design_seed: 20260814\n", "  design_seed: 1\n  extra: 2\n"
            ),
            "keys",
        ),
        (
            lambda text: text.replace("  bootstrap_resamples: 19\n", ""),
            "bootstrap_resamples|analysis",
        ),
        (
            lambda text: text.replace("  confidence_level: 0.95\n", ""),
            "confidence_level|analysis",
        ),
        (
            lambda text: text.replace(
                "bootstrap_resamples: 19", "bootstrap_resamples: true"
            ),
            "bootstrap_resamples",
        ),
        (
            lambda text: text.replace(
                "confidence_level: 0.95", "confidence_level: 1.0"
            ),
            "confidence_level",
        ),
        (
            lambda text: text.replace(
                "completeness_threshold: 1.0", "completeness_threshold: 0.9"
            ),
            "1.0",
        ),
        (
            lambda text: text.replace("values: [101, 102, 103]", "values: [101, 101]"),
            "seed.*distinct|duplicate",
        ),
        (
            lambda text: text.replace("values: [101, 102, 103]", "values: [101, x]"),
            "seed.*int64",
        ),
        (
            lambda text: text.replace("  seed:\n    values: [101, 102, 103]\n", ""),
            "seed",
        ),
        (lambda text: text.replace("values: [x, y]", "values: [x, x]"), "distinct"),
    ],
)
def test_sobol_meta_config_is_strict(tmp_path: Path, change, message: str) -> None:  # type: ignore[no-untyped-def]
    meta, _, _, _ = meta_fixture(tmp_path)
    _sobol_meta(meta)
    meta.write_text(change(meta.read_text()))
    with pytest.raises((TypeError, ValueError), match=message):
        load_meta_config(meta)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("base_sample_size", 2**60, "N\\(2D\\+2\\).*int64"),
        ("bootstrap_resamples", 2**63, "bootstrap_resamples.*int64"),
    ],
)
def test_sobol_meta_config_rejects_non_int64_cardinalities(
    tmp_path: Path,
    field: str,
    value: int,
    message: str,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _sobol_meta(meta, **{field: value})
    with pytest.raises(ValueError, match=message):
        load_meta_config(meta)


@pytest.mark.parametrize("limit", ["resamples", "work"])
def test_sobol_bootstrap_caps_precede_indicator_plugin_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _sobol_meta(
        meta,
        bootstrap_resamples=(
            plan_module.MAX_BOOTSTRAP_RESAMPLES + 1
            if limit == "resamples"
            else 19
        ),
    )
    monkeypatch.setattr(
        "eqasim_sa.adapters.meta_config._indicator_blocks",
        lambda *args: (_ for _ in ()).throw(AssertionError("plugins resolved")),
    )
    if limit == "work":
        monkeypatch.setattr(plan_module, "MAX_BOOTSTRAP_WORK_CELLS", 1)

    with pytest.raises(ValueError, match="operational safe cap"):
        load_meta_config(meta)


def test_sobol_meta_config_requires_at_least_two_seed_values_with_specific_message(
    tmp_path: Path,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _sobol_meta(meta)
    meta.write_text(
        meta.read_text().replace("values: [101, 102, 103]", "values: [101]")
    )
    with pytest.raises(
        ValueError, match="Sobol parameters.seed.values requires at least two"
    ):
        load_meta_config(meta)


def _morris_meta(
    meta: Path, *, trajectories: int = 2, design_seed: int = 20260814
) -> None:
    text = meta.read_text()
    text = text.replace(
        "  method: grid\n  seed_role: replication\n",
        f"  method: morris\n  seed_role: replication\n  trajectories: {trajectories}\n"
        f"  design_seed: {design_seed}\n",
    )
    text = text.replace(
        "  b:\n    values: [x, y]\n",
        "",
    )
    meta.write_text(
        text
        + (
            "artifacts:\n  age: {class: c, synthetic: s, references: [ref]}\n"
            "indicators:\n  - pair: {scalarizer: tv_distance, artifact: age, reference: ref}\n"
        )
    )


def test_grid_schema_order_identity_and_no_mutation(tmp_path: Path) -> None:
    current = design(tmp_path / "campaign")
    before = copy.deepcopy(current)
    result = build_plan(current)
    assert result.status == "created" and result.design_positions == 12
    assert result.unique_runs == 12 and result.path == Path("plan.parquet")
    assert current == before
    table = pq.ParquetFile(tmp_path / "campaign" / "plan.parquet").read()
    assert table.column_names == [
        "designId",
        "method",
        "role",
        "runId",
        "seed",
        "contextHash",
        "a",
        "b",
    ]
    assert table.schema.types == [
        pa.string(),
        pa.string(),
        pa.string(),
        pa.string(),
        pa.int64(),
        pa.string(),
        pa.int64(),
        pa.string(),
    ]
    assert all(not field.nullable for field in table.schema)
    assert table.schema.metadata == {
        b"planSchemaVersion": b"1",
        b"canonicalizationVersion": b"1",
        b"contextDescriptor": b'{"dataManifestSha256":"b","eqasimCommit":"a","eqasimSaVersion":"0.1.0","lockfileSha256":"c"}',
    }
    rows = table.to_pylist()
    preimage = b'{"factors":[{"levelIndex":0,"name":"a"},{"levelIndex":0,"name":"b"}],"replicationIndex":0}'
    assert (
        rows[0]["designId"]
        == hashlib.sha256(b"eqasim-sa:design:grid:v1\0" + preimage).hexdigest()[:32]
    )
    assert rows[0]["runId"] == compute_run_id(
        {
            "working_directory": "/ignored",
            "run": ["stage"],
            "config": {"a": 1, "b": "x", "random_seed": 41},
        },
        41,
    )
    assert [(row["a"], row["b"], row["seed"]) for row in rows[:4]] == [
        (1, "x", 41),
        (1, "x", 42),
        (1, "y", 41),
        (1, "y", 42),
    ]
    assert {row["method"] for row in rows} == {"grid"}
    assert {row["role"] for row in rows} == {"point"}


def test_universal_position_cap_precedes_table_allocation_for_core_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert plan_module.MAX_PLAN_POSITIONS == 1_000_000
    assert plan_module.MAX_PLAN_CELLS == 20_000_000
    monkeypatch.setattr(plan_module, "MAX_PLAN_POSITIONS", 1)
    monkeypatch.setattr(
        plan_module,
        "_table",
        lambda *args: (_ for _ in ()).throw(AssertionError("table allocated")),
    )

    for current in (
        design(tmp_path / "grid"),
        morris_design(tmp_path / "morris"),
        sobol_design(tmp_path / "sobol"),
    ):
        with pytest.raises(ValueError, match="safe cap of 1 design positions"):
            build_plan(current)


def test_universal_cell_cap_precedes_grid_table_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan_module, "MAX_PLAN_CELLS", 95)
    monkeypatch.setattr(
        plan_module,
        "_table",
        lambda *args: (_ for _ in ()).throw(AssertionError("table allocated")),
    )

    with pytest.raises(ValueError, match="safe cap of 95 table cells"):
        build_plan(design(tmp_path / "grid"))


def test_morris_exact_mixed_radix_factoradic_rows_and_metadata(tmp_path: Path) -> None:
    current = morris_design(tmp_path / "campaign")
    before = copy.deepcopy(current)

    result = build_plan(current)

    assert current == before
    assert result.design_positions == 3 * (2 + 1) * 2
    table = pq.read_table(tmp_path / "campaign" / "plan.parquet")
    assert table.column_names == [
        "designId",
        "method",
        "role",
        "runId",
        "seed",
        "contextHash",
        "a",
        "b",
    ]
    assert table.schema.types == [
        pa.string(),
        pa.string(),
        pa.string(),
        pa.string(),
        pa.int64(),
        pa.string(),
        pa.int64(),
        pa.float64(),
    ]
    assert all(not field.nullable for field in table.schema)
    definition = {
        "designSeed": 20260814,
        "factors": [
            {"name": "a", "values": [0, 2, 5]},
            {"name": "b", "values": [-1.0, 1.0]},
        ],
    }
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    domain = b"eqasim-sa:design:morris:v1\0"
    candidate_count = math.factorial(2) * 4 * 2
    offset = (
        int.from_bytes(hashlib.sha256(domain + encoded + b"\0offset").digest(), "big")
        % candidate_count
    )
    stride = (
        int.from_bytes(hashlib.sha256(domain + encoded + b"\0stride").digest(), "big")
        % candidate_count
        or 1
    )
    while math.gcd(stride, candidate_count) != 1:
        stride = (stride + 1) % candidate_count or 1
    rows = table.to_pylist()
    expected = []
    for trajectory in range(3):
        code = (offset + trajectory * stride) % candidate_count
        choices = []
        for levels in (3, 2):
            code, digit = divmod(code, 2 * (levels - 1))
            choices.append((digit // 2, 1 if digit % 2 == 0 else -1))
        permutations = ((0, 1), (1, 0))
        permutation = permutations[code]
        point = [edge if direction == 1 else edge + 1 for edge, direction in choices]
        points = [tuple(point)]
        for factor in permutation:
            point = list(point)
            point[factor] += choices[factor][1]
            points.append(tuple(point))
        for replication, seed in enumerate((41, 42)):
            for rank, indices in enumerate(points):
                role = (
                    f"trajectory:{trajectory}:rank:0:start"
                    if rank == 0
                    else f"trajectory:{trajectory}:rank:{rank}:factor:{('a', 'b')[permutation[rank - 1]]}"
                )
                design_preimage = {
                    "design": {**definition, "trajectories": 3},
                    "replicationIndex": replication,
                    "role": role,
                }
                expected.append(
                    (
                        role,
                        seed,
                        (0, 2, 5)[indices[0]],
                        (-1.0, 1.0)[indices[1]],
                        hashlib.sha256(
                            domain
                            + json.dumps(
                                design_preimage, sort_keys=True, separators=(",", ":")
                            ).encode()
                        ).hexdigest()[:32],
                    )
                )
    assert [
        (row["role"], row["seed"], row["a"], row["b"], row["designId"]) for row in rows
    ] == expected
    assert table.schema.metadata == {
        b"planSchemaVersion": b"1",
        b"canonicalizationVersion": b"1",
        b"contextDescriptor": json.dumps(
            current.context_descriptor, sort_keys=True, separators=(",", ":")
        ).encode(),
        b"morrisDesignVersion": b"1",
        b"morrisDesign": json.dumps(
            {**definition, "trajectories": 3}, sort_keys=True, separators=(",", ":")
        ).encode(),
    }
    assert len({tuple(row["role"].split(":")[:2]) for row in rows}) == 3


def test_morris_is_deterministic_order_independent_and_design_seed_changes_plan(
    tmp_path: Path,
) -> None:
    first = morris_design(tmp_path / "first")
    reordered = morris_design(
        tmp_path / "reordered", factors={"a": [0, 2, 5], "b": [-1.0, 1.0]}
    )
    changed = morris_design(tmp_path / "changed", design_seed=20260815)
    build_plan(first)
    build_plan(reordered)
    build_plan(changed)
    first_bytes = (first.campaign_root / "plan.parquet").read_bytes()
    assert (reordered.campaign_root / "plan.parquet").read_bytes() == first_bytes
    assert (changed.campaign_root / "plan.parquet").read_bytes() != first_bytes
    replay = build_plan(first)
    assert replay.status == "unchanged"
    assert (first.campaign_root / "plan.parquet").read_bytes() == first_bytes


def test_morris_trajectories_are_distinct_adjacent_and_keep_crn(tmp_path: Path) -> None:
    current = morris_design(tmp_path / "campaign", trajectories=8)
    result = build_plan(current)
    rows = pq.read_table(current.campaign_root / "plan.parquet").to_pylist()
    blocks = [rows[index : index + 3] for index in range(0, len(rows), 3)]
    assert result.design_positions == 8 * 3 * 2
    assert (
        len({tuple((row["a"], row["b"]) for row in block) for block in blocks[::2]})
        == 8
    )
    for block in blocks:
        assert len({row["seed"] for row in block}) == 1
        assert [int(row["role"].split(":")[3]) for row in block] == [0, 1, 2]
        for previous, current_row in zip(block, block[1:]):
            factor = current_row["role"].split(":")[-1]
            changed = [
                name for name in ("a", "b") if previous[name] != current_row[name]
            ]
            assert changed == [factor]
            values = current.factors[factor]
            assert (
                abs(values.index(previous[factor]) - values.index(current_row[factor]))
                == 1
            )


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"trajectories": 0}, "trajectories"),
        ({"trajectories": True}, "trajectories"),
        ({"trajectories": 17}, "candidate"),
        ({"design_seed": 2**63}, "design_seed"),
        ({"design_seed": True}, "design_seed"),
        ({"factors": {"a": [1]}}, "two"),
        ({"factors": {"a": [1, 1]}}, "distinct|increasing"),
        ({"factors": {"a": [2, 1]}}, "increasing"),
        ({"factors": {"a": [1, 2.0]}}, "same type"),
        ({"factors": {"a": [True, False]}}, "numeric"),
        ({"factors": {"a": ["x", "y"]}}, "numeric"),
        ({"factors": {"a": [1.0, float("inf")]}}, "finite"),
        (
            {"factors": {"a": [-1e308, 1e308]}, "trajectories": 2},
            "range|delta|representable",
        ),
        ({"factors": {"a": [0.0, 5e-324, 1e308]}}, "range|delta|representable"),
    ],
)
def test_morris_core_rejects_invalid_contract(
    tmp_path: Path, changes: dict, message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        build_plan(morris_design(tmp_path / "campaign", **changes))


def test_morris_meta_config_preserves_explicit_design_contract(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _morris_meta(meta)

    loaded = load_meta_config(meta)

    assert loaded.method == "morris"
    assert loaded.trajectories == 2
    assert loaded.design_seed == 20260814
    assert loaded.seed_role == "replication"
    assert loaded.seeds == (41, 42)


def test_morris_meta_config_requires_at_least_one_indicator(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    _morris_meta(meta)
    text = meta.read_text()
    meta.write_text(text[: text.index("artifacts:\n")])
    with pytest.raises(ValueError, match="Morris.*indicator"):
        load_meta_config(meta)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda text: text.replace("  trajectories: 2\n", ""), "trajectories"),
        (lambda text: text.replace("  design_seed: 20260814\n", ""), "design_seed"),
        (
            lambda text: text.replace(
                "  design_seed: 20260814\n", "  design_seed: 1\n  extra: 2\n"
            ),
            "keys|replication",
        ),
        (
            lambda text: text.replace(
                "completeness_threshold: 1.0", "completeness_threshold: 0.5"
            ),
            "1.0",
        ),
        (lambda text: text.replace("values: [1, 2]", "values: [2, 1]"), "increasing"),
        (
            lambda text: text.replace("values: [1, 2]", "values: [1, 1]"),
            "distinct|increasing",
        ),
        (lambda text: text.replace("values: [1, 2]", "values: [one, two]"), "numeric"),
        (lambda text: text.replace("seeds: [41, 42]", "seeds: []"), "seeds"),
        (
            lambda text: text.replace("seeds: [41, 42]", "seeds: [41, 41]"),
            "duplicate|unique",
        ),
        (lambda text: text.replace("seeds: [41, 42]", "seeds: [true]"), "int64"),
        (lambda text: text.replace("trajectories: 2", "trajectories: 3"), "candidate"),
        (
            lambda text: text.replace(
                "values: [1, 2]", "values: [-1.0e+308, 1.0e+308]"
            ),
            "range|delta|representable",
        ),
        (
            lambda text: text.replace(
                "values: [1, 2]", "values: [0.0, 5.0e-324, 1.0e+308]"
            ),
            "range|delta|representable",
        ),
    ],
)
def test_morris_meta_config_is_strict(tmp_path: Path, change, message: str) -> None:  # type: ignore[no-untyped-def]
    meta, _, _, _ = meta_fixture(tmp_path)
    _morris_meta(meta)
    meta.write_text(change(meta.read_text()))
    with pytest.raises((TypeError, ValueError), match=message):
        load_meta_config(meta)


def test_duplicates_preserve_exact_plan_and_memoize_resolved_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"resolve": 0, "run_id": 0}
    real_resolve = plan_module.resolve_config
    real_compute = plan_module.compute_run_id

    def counted_resolve(*args):  # type: ignore[no-untyped-def]
        calls["resolve"] += 1
        return real_resolve(*args)

    def counted_compute(*args):  # type: ignore[no-untyped-def]
        calls["run_id"] += 1
        return real_compute(*args)

    monkeypatch.setattr(plan_module, "resolve_config", counted_resolve)
    monkeypatch.setattr(plan_module, "compute_run_id", counted_compute)
    current = design(tmp_path / "campaign", factors={"a": [1, 1]}, seeds=(4, 5))
    first = build_plan(current)
    path = tmp_path / "campaign" / "plan.parquet"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert calls == {"resolve": 2, "run_id": 2}
    assert hashlib.sha256(before).hexdigest() == (
        "ac0869dd6d8d25778c5d7ed1c4b1470985ee1e4c8ee7d7b2e2445152b871a35a"
    )
    second = build_plan(current)
    rows = pq.read_table(path).to_pylist()
    assert calls == {"resolve": 4, "run_id": 4}
    assert first.status == "created" and second.status == "unchanged"
    assert len({row["designId"] for row in rows}) == 4
    assert rows[0]["runId"] == rows[2]["runId"]
    assert rows[0]["runId"] != rows[1]["runId"]
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_memoization_key_distinguishes_signed_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real_compute = plan_module.compute_run_id

    def counted_compute(*args):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real_compute(*args)

    monkeypatch.setattr(plan_module, "compute_run_id", counted_compute)
    current = design(
        tmp_path / "campaign", factors={"a": [-0.0, 0.0, -0.0]}, seeds=(4,)
    )
    build_plan(current)
    rows = pq.read_table(current.campaign_root / "plan.parquet").to_pylist()

    assert calls == 2
    assert rows[0]["runId"] != rows[1]["runId"]
    assert rows[0]["runId"] == rows[2]["runId"]


def test_factor_mapping_order_is_ignored_but_level_order_changes_design(
    tmp_path: Path,
) -> None:
    left = build_plan(design(tmp_path / "left", factors={"b": ["x", "y"], "a": [1]}))
    right = build_plan(design(tmp_path / "right", factors={"a": [1], "b": ["x", "y"]}))
    reversed_levels = build_plan(
        design(tmp_path / "reversed", factors={"a": [1], "b": ["y", "x"]})
    )
    left_table = pq.read_table(tmp_path / "left" / left.path)
    right_table = pq.read_table(tmp_path / "right" / right.path)
    reversed_table = pq.read_table(tmp_path / "reversed" / reversed_levels.path)
    assert left_table.equals(right_table)
    assert set(left_table["runId"].to_pylist()) == set(
        reversed_table["runId"].to_pylist()
    )
    assert left_table["designId"].to_pylist() == reversed_table["designId"].to_pylist()
    assert left_table["runId"].to_pylist() != reversed_table["runId"].to_pylist()


def test_conflict_state_and_unsafe_plan_are_never_overwritten(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    build_plan(design(campaign, factors={"a": [1]}))
    before = (campaign / "plan.parquet").read_bytes()
    with pytest.raises(FileExistsError, match="campaign_root"):
        build_plan(design(campaign, factors={"a": [2]}))
    assert (campaign / "plan.parquet").read_bytes() == before
    for state in ("runs", "archives", "logs", "indicators.parquet"):
        root = tmp_path / state
        root.mkdir()
        target = root / state
        target.mkdir() if "." not in state else target.write_bytes(b"x")
        with pytest.raises(FileExistsError):
            build_plan(design(root, factors={"a": [1]}))
    link_root = tmp_path / "link-root"
    link_root.mkdir()
    (link_root / "plan.parquet").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="regular"):
        build_plan(design(link_root, factors={"a": [1]}))


@pytest.mark.parametrize(
    "factors,seeds,message",
    [
        ({}, (1,), "factor"),
        ({"designId": [1]}, (1,), "reserved"),
        ({"campaign": ["other"]}, (1,), "reserved"),
        ({"a": []}, (1,), "non-empty"),
        ({"a": [True]}, (1,), "scalar"),
        ({"a": [1, 1.0]}, (1,), "same type"),
        ({"a": [2**63]}, (1,), "int64"),
        ({"a": [float("inf")]}, (1,), "finite"),
        ({"a": [""]}, (1,), "non-empty"),
        ({"a": ["x\0"]}, (1,), "NUL"),
        ({"a": [1]}, (), "seed"),
        ({"a": [1]}, (1, 1), "unique"),
        ({"a": [1]}, (True,), "int64"),
    ],
)
def test_core_rejects_invalid_contract(
    tmp_path: Path, factors, seeds, message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        build_plan(design(tmp_path / "campaign", factors=factors, seeds=seeds))


@pytest.mark.parametrize(
    "requirements",
    [
        {"other": 1},
        {"config": []},
        {"config": {"a": 2}},
        {"config": {"random_seed": 2}},
    ],
)
def test_core_rejects_invalid_requirements(tmp_path: Path, requirements: dict) -> None:
    with pytest.raises((TypeError, ValueError), match="requirements"):
        build_plan(design(tmp_path / "campaign", requirements=requirements))


def test_context_descriptor_must_match_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    original = load_meta_config(meta)
    from dataclasses import replace

    import eqasim_sa.adapters.application as application

    monkeypatch.setattr(
        application,
        "load_meta_config",
        lambda path: replace(original, context_hash="c" * 64),
    )
    with pytest.raises(ValueError, match="descriptor"):
        application.plan_from_meta_config(meta)


def test_run_id_collision_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "eqasim_sa.core.plan.compute_run_id", lambda config, seed: "0" * 32
    )
    with pytest.raises(RuntimeError, match="runId collision"):
        build_plan(design(tmp_path / "campaign", factors={"a": [1, 2]}, seeds=(1,)))
    assert not (tmp_path / "campaign" / "plan.parquet").exists()


def test_requirements_infrastructure_is_rejected_by_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    monkeypatch.setattr(
        "eqasim_sa.adapters.application.eqasim_requirements",
        lambda indicators: {"config": {"output_path": "/tmp"}},
    )
    with pytest.raises(ValueError, match="infrastructure"):
        plan_from_meta_config(meta)


def test_design_id_collision_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.plan as plan_module

    real = plan_module.hashlib.sha256

    class Collision:
        def hexdigest(self) -> str:
            return "0" * 64

    monkeypatch.setattr(
        plan_module.hashlib,
        "sha256",
        lambda data: Collision() if data.startswith(plan_module.DOMAIN) else real(data),
    )
    with pytest.raises(RuntimeError, match="collision"):
        build_plan(design(tmp_path / "campaign", factors={"a": [1, 2]}, seeds=(1,)))
    assert not (tmp_path / "campaign" / "plan.parquet").exists()


def test_atomic_failure_leaves_no_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eqasim_sa.core.plan as plan_module

    monkeypatch.setattr(
        plan_module,
        "write_parquet_exclusive",
        lambda table, path, **options: (_ for _ in ()).throw(OSError("injected")),
    )
    with pytest.raises(OSError, match="injected"):
        build_plan(design(tmp_path / "campaign"))
    assert not (tmp_path / "campaign" / "plan.parquet").exists()


def test_concurrent_equivalent_plans_use_no_clobber_without_parent_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    current = design(campaign)
    barrier = threading.Barrier(2)
    real_publish = plan_module.write_parquet_exclusive
    results = []
    errors = []

    def synchronized_publish(table, path, **options):  # type: ignore[no-untyped-def]
        barrier.wait(5)
        return real_publish(table, path, **options)

    def publish() -> None:
        try:
            results.append(build_plan(current))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(
        plan_module, "write_parquet_exclusive", synchronized_publish
    )
    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(result.status for result in results) == ["created", "unchanged"]
    assert not tuple(campaign.parent.glob(".eqasim-sa-*.lock"))


def test_campaign_root_with_arbitrary_state_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "notes.txt").write_text("old state")
    with pytest.raises(FileExistsError, match="durable state"):
        build_plan(design(root, factors={"a": [1]}))


def test_yaml_loader_rejects_alias_duplicate_non_string_and_non_finite(
    tmp_path: Path,
) -> None:
    cases = ["a: 1\na: 2\n", "a: &x [1]\nb: *x\n", "1: value\n", "a: .nan\n"]
    for index, text in enumerate(cases):
        path = tmp_path / f"{index}.yml"
        path.write_text(text)
        with pytest.raises((TypeError, ValueError)):
            load_yaml_document(path)


def git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def meta_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    model = tmp_path / "model"
    model.mkdir()
    git("init", "-q", cwd=model)
    git("config", "user.email", "test@example.test", cwd=model)
    git("config", "user.name", "Test", cwd=model)
    config = model / "config.yml"
    config.write_text(
        "working_directory: /ignored\nrun: [old]\nconfig:\n  a: 0\n  b: x\n  random_seed: 9\n"
    )
    lock = model / "lock.yml"
    lock.write_text("locked\n")
    git("add", "config.yml", "lock.yml", cwd=model)
    git("commit", "-qm", "fixture", cwd=model)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n")
    meta = tmp_path / "meta.yml"
    meta.write_text(
        "campaign: demo\ncampaign_root: campaign\nmodel:\n  config: model/config.yml\n  root: model\n"
        "context:\n  data_manifest: manifest.json\n  lockfile: model/lock.yml\n"
        "analysis:\n  completeness_threshold: 1.0\n"
        "experiment:\n  method: grid\n  seed_role: replication\n  replications:\n    seeds: [41, 42]\n"
        "parameters:\n  a:\n    values: [1, 2]\n  b:\n    values: [x, y]\n"
    )
    return meta, model, config, manifest


@pytest.mark.parametrize(
    "value",
    ["missing", "true", "1", ".nan", ".inf", "-0.1", "1.1"],
)
def test_meta_config_requires_finite_float_completeness_threshold(
    tmp_path: Path, value: str
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    text = meta.read_text()
    if value == "missing":
        text = text.replace("analysis:\n  completeness_threshold: 1.0\n", "")
    else:
        text = text.replace(
            "completeness_threshold: 1.0", f"completeness_threshold: {value}"
        )
    meta.write_text(text)
    with pytest.raises(
        (TypeError, ValueError),
        match="completeness_threshold|analysis|JSON-compatible",
    ):
        load_meta_config(meta)


@pytest.mark.parametrize("value", ["0.0", "0.5", "1.0"])
def test_meta_config_accepts_boundary_completeness_threshold(
    tmp_path: Path, value: str
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text().replace(
            "completeness_threshold: 1.0", f"completeness_threshold: {value}"
        )
    )
    assert load_meta_config(meta).completeness_threshold == float(value)


@pytest.mark.parametrize("replacement", ["", "  seed_role: noise\n"])
def test_meta_config_requires_valid_explicit_seed_role(
    tmp_path: Path, replacement: str
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(meta.read_text().replace("  seed_role: replication\n", replacement))
    with pytest.raises(ValueError, match="seed_role"):
        load_meta_config(meta)


def test_seed_factor_and_replication_conflict_uses_normative_rule(
    tmp_path: Path,
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text().replace(
            "parameters:\n",
            "parameters:\n  seed:\n    values: [11, 12]\n",
        )
    )
    with pytest.raises(
        ValueError,
        match="^le seed est soit un facteur, soit une réplication, jamais les deux$",
    ):
        load_meta_config(meta)


def test_grid_seed_factor_is_unambiguous_but_not_supported_yet(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    text = meta.read_text().replace(
        "  seed_role: replication\n", "  seed_role: factor\n"
    )
    text = text.replace("  replications:\n    seeds: [41, 42]\n", "")
    text = text.replace("parameters:\n", "parameters:\n  seed:\n    values: [11, 12]\n")
    meta.write_text(text)
    with pytest.raises(ValueError, match="grid.*seed factor.*not supported"):
        load_meta_config(meta)


def test_meta_context_and_application_end_to_end(tmp_path: Path) -> None:
    meta, model, config, manifest = meta_fixture(tmp_path)
    checksums = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (meta, config, manifest, model / "lock.yml")
    ]
    loaded = load_meta_config(meta)
    assert loaded.model_root == model.resolve()
    assert loaded.context_descriptor["eqasimCommit"] == git(
        "rev-parse", "--verify", "HEAD", cwd=model
    )
    expected = hashlib.sha256(
        b"eqasim-sa:context:v1\0"
        + json.dumps(
            loaded.context_descriptor,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert loaded.context_hash == expected
    result = plan_from_meta_config(meta)
    assert result.status == "created" and result.design_positions == 8
    assert [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (meta, config, manifest, model / "lock.yml")
    ] == checksums
    metadata = pq.ParquetFile(tmp_path / "campaign" / "plan.parquet").metadata
    assert {
        metadata.row_group(r).column(c).compression
        for r in range(metadata.num_row_groups)
        for c in range(metadata.row_group(r).num_columns)
    } == {"ZSTD"}


def test_meta_rejects_dirty_repo_untracked_config_symlinks_and_nested_campaign(
    tmp_path: Path,
) -> None:
    meta, model, config, manifest = meta_fixture(tmp_path)
    config.write_text(config.read_text() + "# dirty\n")
    with pytest.raises(ValueError, match="clean"):
        load_meta_config(meta)
    git("checkout", "--", "config.yml", cwd=model)
    (model / "untracked").write_text("x")
    with pytest.raises(ValueError, match="clean"):
        load_meta_config(meta)
    (model / "untracked").unlink()
    meta.write_text(
        meta.read_text().replace(
            "campaign_root: campaign", "campaign_root: model/campaign"
        )
    )
    with pytest.raises(ValueError, match="outside"):
        load_meta_config(meta)
    meta.write_text(
        meta.read_text().replace(
            "campaign_root: model/campaign", "campaign_root: campaign"
        )
    )
    manifest.unlink()
    manifest.symlink_to(model / "lock.yml")
    with pytest.raises(ValueError, match="symlink"):
        load_meta_config(meta)


def test_campaign_root_parent_segments_cannot_escape_into_model(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text().replace(
            "campaign_root: campaign", "campaign_root: outside/../model/campaign"
        )
    )
    with pytest.raises(ValueError, match="outside"):
        load_meta_config(meta)


def test_seedless_eqasim_document_is_fully_validated(tmp_path: Path) -> None:
    meta, _, config, _ = meta_fixture(tmp_path)
    config.write_text("config:\n  a: 0\n  b: x\n")
    git("add", "config.yml", cwd=config.parent)
    git("commit", "-qm", "invalid config", cwd=config.parent)
    with pytest.raises(TypeError, match="working_directory"):
        load_meta_config(meta)


def test_each_context_input_changes_context_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta, model, _, manifest = meta_fixture(tmp_path)
    baseline = load_meta_config(meta).context_hash
    manifest.write_text("changed\n")
    assert load_meta_config(meta).context_hash != baseline
    manifest.write_text("{}\n")
    (model / "lock.yml").write_text("changed\n")
    git("add", "lock.yml", cwd=model)
    git("commit", "-qm", "lock", cwd=model)
    changed_commit_and_lock = load_meta_config(meta).context_hash
    assert changed_commit_and_lock != baseline
    real_version = __import__(
        "eqasim_sa.adapters.meta_config", fromlist=["importlib"]
    ).importlib.metadata.version
    monkeypatch.setattr(
        "eqasim_sa.adapters.meta_config.importlib.metadata.version",
        lambda name: "9.9.9" if name == "eqasim-sa" else real_version(name),
    )
    assert load_meta_config(meta).context_hash != changed_commit_and_lock


@pytest.mark.parametrize("missing", ["manifest", "lock"])
def test_context_rejects_missing_files(tmp_path: Path, missing: str) -> None:
    meta, model, _, manifest = meta_fixture(tmp_path)
    (manifest if missing == "manifest" else model / "lock.yml").unlink()
    if missing == "lock":
        git("add", "lock.yml", cwd=model)
        git("commit", "-qm", "remove lock", cwd=model)
    with pytest.raises(FileNotFoundError):
        load_meta_config(meta)


def test_requirements_are_pure_and_delegated() -> None:
    indicators = [IndicatorRequirement("a", "age")]
    assert eqasim_requirements(indicators) == {
        "run": ["synthesis.output", "analysis.synthesis.population"]
    }
    assert indicators == [IndicatorRequirement("a", "age")]
    simulator = object.__new__(EqasimSimulator)
    assert simulator.requirements(indicators) == eqasim_requirements(indicators)


def test_eqasim_requirements_reject_unknown_artifact_with_indicator_name() -> None:
    with pytest.raises(ValueError, match="custom.*unknown"):
        eqasim_requirements((IndicatorRequirement("custom", "unknown"),))


def test_meta_config_parses_strict_artifacts_and_three_indicator_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads = []

    class EntryPoint:
        group = "eqasim_sa.indicators"
        name = "extension"
        value = "tests.test_plan:extension"
        dist = _PluginDistribution()

        def load(self):  # type: ignore[no-untyped-def]
            loads.append(self.name)
            return lambda artifacts: 1.0

    class EntryPoints(tuple):
        def select(self, **selection):  # type: ignore[no-untyped-def]
            return self if selection == {"group": "eqasim_sa.indicators"} else ()

    monkeypatch.setattr(
        "eqasim_sa.adapters.meta_config.importlib.metadata.entry_points",
        lambda: EntryPoints((EntryPoint(),)),
    )
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        + (
            "artifacts:\n"
            "  age:\n    class: Age class\n    synthetic: Proportion_EQASIM\n    references: [INSEE]\n"
            "  distance:\n    class: Distance class\n    synthetic: Proportion_EQASIM\n    references: [HTS]\n"
            "indicators:\n"
            "  - pair: {scalarizer: tv_distance, artifact: age, reference: INSEE}\n"
            "  - product: {scalarizers: [tv_distance], artifacts: [distance]}\n"
            "  - custom: {function: extension, artifact: age}\n"
        )
    )
    loaded = load_meta_config(meta)
    assert loads == []
    assert loaded.indicator_requirements == (
        IndicatorRequirement("tv_distance(age, ref=INSEE)", "age"),
        IndicatorRequirement("tv_distance(distance, ref=HTS)", "distance"),
        IndicatorRequirement("extension(age)", "age"),
    )


@pytest.mark.parametrize(
    "addition",
    [
        "artifacts: []\nindicators: []\n",
        "artifacts:\n  age: {class: c, synthetic: s, references: [], extra: x}\nindicators: []\n",
        "artifacts:\n  age: {class: c, synthetic: s, references: []}\nindicators:\n  - pair: {scalarizer: tv_distance, artifact: age}\n",
        "artifacts:\n  age: {class: c, synthetic: s, references: []}\nindicators:\n  - pair: {}\n    custom: {}\n",
    ],
)
def test_meta_config_rejects_malformed_artifacts_and_indicators(
    tmp_path: Path, addition: str
) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(meta.read_text() + addition)
    with pytest.raises((TypeError, ValueError), match="artifact|indicator|pair"):
        load_meta_config(meta)


def test_custom_indicator_must_be_registered_by_entry_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eqasim_sa.adapters.meta_config.importlib.metadata.entry_points",
        lambda: type("Entries", (tuple,), {"select": lambda self, **kwargs: ()})(),
    )
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        + (
            "artifacts:\n  age: {class: c, synthetic: s, references: []}\n"
            "indicators:\n  - custom: {function: missing, artifact: age}\n"
        )
    )
    with pytest.raises(ValueError, match="missing.*eqasim_sa.indicators"):
        load_meta_config(meta)


def test_custom_indicator_entry_point_class_is_instantiated_without_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads = []

    class Extension:
        def compute(self, artifacts):  # type: ignore[no-untyped-def]
            return 1.0

    class EntryPoint:
        group = "eqasim_sa.indicators"
        name = "extension"
        value = "tests.test_plan:extension"
        dist = _PluginDistribution()

        def load(self):  # type: ignore[no-untyped-def]
            loads.append(self.name)
            return Extension

    class EntryPoints(tuple):
        def select(self, **selection):  # type: ignore[no-untyped-def]
            return self if selection == {"group": "eqasim_sa.indicators"} else ()

    monkeypatch.setattr(
        "eqasim_sa.adapters.meta_config.importlib.metadata.entry_points",
        lambda: EntryPoints((EntryPoint(),)),
    )
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        + (
            "artifacts:\n  age: {class: c, synthetic: s, references: []}\n"
            "indicators:\n  - custom: {function: extension, artifact: age}\n"
        )
    )
    loaded = load_meta_config(meta)
    assert loads == []
    artifact = tmp_path / "age.parquet"
    pq.write_table(pa.table({"value": [1]}), artifact)
    assert loaded.indicators[0].compute(ArtifactSet({"age": artifact})) == 1.0
    assert loads == ["extension"]


def test_custom_indicator_entry_point_class_requiring_arguments_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Extension:
        def __init__(self, required):  # type: ignore[no-untyped-def]
            self.required = required

        def compute(self, artifacts):  # type: ignore[no-untyped-def]
            return 1.0

    class EntryPoint:
        group = "eqasim_sa.indicators"
        name = "extension"
        value = "tests.test_plan:extension"
        dist = _PluginDistribution()

        def load(self):  # type: ignore[no-untyped-def]
            return Extension

    class EntryPoints(tuple):
        def select(self, **selection):  # type: ignore[no-untyped-def]
            return self if selection == {"group": "eqasim_sa.indicators"} else ()

    monkeypatch.setattr(
        "eqasim_sa.adapters.meta_config.importlib.metadata.entry_points",
        lambda: EntryPoints((EntryPoint(),)),
    )
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(
        meta.read_text()
        + (
            "artifacts:\n  age: {class: c, synthetic: s, references: []}\n"
            "indicators:\n  - custom: {function: extension, artifact: age}\n"
        )
    )
    loaded = load_meta_config(meta)
    artifact = tmp_path / "age.parquet"
    pq.write_table(pa.table({"value": [1]}), artifact)
    with pytest.raises(TypeError, match="extension.*without arguments"):
        loaded.indicators[0].compute(ArtifactSet({"age": artifact}))
