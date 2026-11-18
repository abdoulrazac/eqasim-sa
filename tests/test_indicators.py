import math
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eqasim_sa.core.indicators import (
    ArtifactDeclaration,
    ArtifactSet,
    PairDeclaration,
    ProductDeclaration,
    TableArtifact,
    TotalVariationIndicator,
    resolve_indicators,
)
from tests.conftest import make_symlink


def archive(tmp_path: Path, **columns: list[object]) -> Path:
    path = tmp_path / "distribution.parquet"
    pq.write_table(pa.table(columns), path, compression="zstd")
    return path


def indicator(
    reference: str = "reference", carries_reference: bool = True
) -> TotalVariationIndicator:
    artifact = TableArtifact(
        "distribution",
        "class",
        "synthetic",
        carries_reference,
    )
    return TotalVariationIndicator(artifact, reference)


@pytest.mark.parametrize(
    ("reference", "synthetic", "expected"),
    [
        ([0.5, 0.5], [0.5, 0.5], 0.0),
        ([1.0, 0.0], [0.0, 1.0], 1.0),
        ([0.4, 0.6], [0.5, 0.5], 0.1),
        ([40.0, 60.0], [400.0, 600.0], 0.0),
    ],
)
def test_total_variation_normalizes_archived_masses(
    tmp_path: Path,
    reference: list[float],
    synthetic: list[float],
    expected: float,
) -> None:
    path = archive(
        tmp_path,
        **{"class": ["a", "b"], "synthetic": synthetic, "reference": reference},
    )
    value = indicator().compute(ArtifactSet({"distribution": path}))
    assert isinstance(value, float)
    assert value == pytest.approx(expected)
    assert 0.0 <= value <= 1.0


@pytest.mark.parametrize(
    ("synthetic", "reference"),
    [([1.0, 0.0], [0.0, 1.0]), ([0.0, 0.0], [0.0, 0.0])],
)
def test_total_variation_rejects_duplicate_classes_before_normalization(
    tmp_path: Path,
    synthetic: list[float],
    reference: list[float],
) -> None:
    path = archive(
        tmp_path,
        **{
            "class": ["same", "same"],
            "synthetic": synthetic,
            "reference": reference,
        },
    )

    with pytest.raises(ValueError, match="duplicate class labels"):
        indicator().compute(ArtifactSet({"distribution": path}))


def test_proportional_references_have_distinct_identities_but_equal_tv(
    tmp_path: Path,
) -> None:
    path = archive(
        tmp_path,
        **{
            "class": ["a", "b"],
            "synthetic": [45.8, 54.2],
            "HTS": [40.5, 59.5],
            "HTS globally rescaled": [81.0, 119.0],
        },
    )
    artifacts = ArtifactSet({"distribution": path})
    raw = indicator("HTS")
    rescaled = indicator("HTS globally rescaled")

    assert raw.identity != rescaled.identity
    assert raw.compute(artifacts) == pytest.approx(rescaled.compute(artifacts))


def test_shape_changing_references_produce_distinct_indicators(tmp_path: Path) -> None:
    path = archive(
        tmp_path,
        **{
            "class": ["a", "b"],
            "synthetic": [45.8, 54.2],
            "HTS": [40.5, 59.5],
            "HTS adjusted": [46.1, 53.9],
        },
    )
    artifacts = ArtifactSet({"distribution": path})
    raw = indicator("HTS")
    adjusted = indicator("HTS adjusted")
    assert raw.identity != adjusted.identity
    assert raw.name == "tv_distance(distribution, ref=HTS)"
    assert adjusted.name == "tv_distance(distribution, ref=HTS adjusted)"
    assert raw.compute(artifacts) != adjusted.compute(artifacts)


def test_distance_without_reference_is_rejected_before_reading(tmp_path: Path) -> None:
    artifact = TableArtifact("distribution", "class", "synthetic", False)
    with pytest.raises(ValueError, match="does not carry a reference"):
        TotalVariationIndicator(artifact, "reference")
    assert tuple(tmp_path.iterdir()) == ()


def test_normalization_is_stable_for_large_finite_masses(tmp_path: Path) -> None:
    path = archive(
        tmp_path,
        **{
            "class": ["a", "b"],
            "synthetic": [1e308, 1e308],
            "reference": [1e308, 1e308],
        },
    )
    assert indicator().compute(ArtifactSet({"distribution": path})) == pytest.approx(
        0.0, abs=1e-15
    )


def test_decimal_masses_are_numeric(tmp_path: Path) -> None:
    path = tmp_path / "distribution.parquet"
    table = pa.table(
        {
            "class": ["a", "b"],
            "synthetic": pa.array(
                [Decimal("40"), Decimal("60")], type=pa.decimal128(4, 0)
            ),
            "reference": pa.array(
                [Decimal("0.4"), Decimal("0.6")], type=pa.decimal128(4, 1)
            ),
        }
    )
    pq.write_table(table, path)
    assert indicator().compute(ArtifactSet({"distribution": path})) == pytest.approx(
        0.0, abs=1e-15
    )


@pytest.mark.parametrize(
    "field", ["artifact", "class", "synthetic", "reference", "capability"]
)
def test_rejects_malformed_declarations(field: str) -> None:
    values: dict[str, object] = {
        "artifact": "distribution",
        "class": "class",
        "synthetic": "synthetic",
        "reference": "reference",
        "capability": True,
    }
    values[field] = 1
    with pytest.raises(ValueError):
        artifact = TableArtifact(
            values["artifact"],
            values["class"],
            values["synthetic"],
            values["capability"],  # type: ignore[arg-type]
        )
        TotalVariationIndicator(artifact, values["reference"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("columns", "message"),
    [
        ({"synthetic": [0.5, 0.5], "reference": [0.5, 0.5]}, "class"),
        ({"class": ["a", "b"], "reference": [0.5, 0.5]}, "synthetic"),
        ({"class": ["a", "b"], "synthetic": [0.5, 0.5]}, "reference"),
        (
            {"class": ["a", None], "synthetic": [0.5, 0.5], "reference": [0.5, 0.5]},
            "null",
        ),
        (
            {"class": ["a", "b"], "synthetic": [0.5, None], "reference": [0.5, 0.5]},
            "null",
        ),
        (
            {"class": ["a", "b"], "synthetic": [0.5, -0.5], "reference": [0.5, 0.5]},
            "non-negative",
        ),
        (
            {
                "class": ["a", "b"],
                "synthetic": [0.5, math.inf],
                "reference": [0.5, 0.5],
            },
            "finite",
        ),
        (
            {"class": ["a", "b"], "synthetic": [0.0, 0.0], "reference": [0.5, 0.5]},
            "positive sum",
        ),
        (
            {"class": ["a", "b"], "synthetic": ["x", "y"], "reference": [0.5, 0.5]},
            "numeric",
        ),
    ],
)
def test_rejects_invalid_archived_columns(
    tmp_path: Path,
    columns: dict[str, list[object]],
    message: str,
) -> None:
    path = archive(tmp_path, **columns)
    with pytest.raises(ValueError, match=message):
        indicator().compute(ArtifactSet({"distribution": path}))


def test_artifact_set_rejects_missing_symlink_and_non_parquet(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        ArtifactSet({"distribution": tmp_path / "missing.parquet"})
    real = archive(tmp_path, **{"class": ["a"], "synthetic": [1], "reference": [1]})
    link = tmp_path / "linked.parquet"
    make_symlink(link, real)
    with pytest.raises(ValueError, match="symlink"):
        ArtifactSet({"distribution": link})
    text = tmp_path / "distribution.txt"
    text.write_text("not parquet")
    with pytest.raises(ValueError, match="parquet"):
        ArtifactSet({"distribution": text})


def test_artifact_set_rejects_symlinked_parent_and_replacement(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    path = archive(real_parent, **{"class": ["a"], "synthetic": [1], "reference": [1]})
    linked_parent = tmp_path / "linked"
    make_symlink(linked_parent, real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ArtifactSet({"distribution": linked_parent / path.name})

    artifacts = ArtifactSet({"distribution": path})
    path.unlink()
    make_symlink(path, tmp_path / "missing.parquet")
    with pytest.raises(ValueError, match="cannot be read"):
        indicator().compute(artifacts)


def test_corrupt_parquet_has_an_archive_error(tmp_path: Path) -> None:
    path = tmp_path / "distribution.parquet"
    path.write_bytes(b"not parquet")
    with pytest.raises(ValueError, match="cannot be read as parquet"):
        indicator().compute(ArtifactSet({"distribution": path}))


def test_computes_from_isolated_archive(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    path = archive(isolated, **{"class": ["a"], "synthetic": [4], "reference": [2]})
    assert tuple(isolated.iterdir()) == (path,)
    assert indicator().compute(ArtifactSet({"distribution": path})) == 0.0


def test_pair_and_product_resolve_to_one_uniform_sequence() -> None:
    artifacts = {
        "age": ArtifactDeclaration("Age class", "Proportion_EQASIM", ("INSEE",)),
        "distance": ArtifactDeclaration(
            "Distance class", "Proportion_EQASIM", ("HTS", "adjusted")
        ),
    }
    resolved = resolve_indicators(
        (
            PairDeclaration("tv_distance", "age", "INSEE"),
            ProductDeclaration(("tv_distance",), ("distance",)),
        ),
        artifacts,
        {
            "tv_distance": lambda artifact, reference: TotalVariationIndicator(
                artifact, reference
            )
        },
    )
    assert [item.name for item in resolved] == [
        "tv_distance(age, ref=INSEE)",
        "tv_distance(distance, ref=HTS)",
        "tv_distance(distance, ref=adjusted)",
    ]


def test_indicator_checks_declared_artifact_before_reading() -> None:
    with pytest.raises(ValueError, match="artifact 'distribution' is not available"):
        indicator().compute(ArtifactSet({}))


def test_product_names_every_invalid_combination() -> None:
    artifacts = {
        "plain": ArtifactDeclaration("class", "synthetic", ()),
        "missing": ArtifactDeclaration("class", "synthetic", ("reference",)),
    }
    with pytest.raises(ValueError) as failure:
        resolve_indicators(
            (ProductDeclaration(("unknown", "tv_distance"), ("absent", "plain")),),
            artifacts,
            {
                "tv_distance": lambda artifact, reference: TotalVariationIndicator(
                    artifact, reference
                )
            },
        )
    message = str(failure.value)
    assert all(
        value in message
        for value in (
            "unknown(absent)",
            "unknown(plain)",
            "tv_distance(absent)",
            "tv_distance(plain)",
        )
    )


def test_product_names_every_reference_for_unknown_scalarizer() -> None:
    artifacts = {
        "distance": ArtifactDeclaration("class", "synthetic", ("HTS", "adjusted")),
    }
    with pytest.raises(ValueError) as failure:
        resolve_indicators(
            (ProductDeclaration(("unknown",), ("distance",)),),
            artifacts,
            {},
        )
    assert "unknown(distance, ref=HTS)" in str(failure.value)
    assert "unknown(distance, ref=adjusted)" in str(failure.value)
