"""Indicator kinds accepted by the meta-config."""

from pathlib import Path

import pytest
import yaml

from eqasim_sa.adapters.application import plan_from_meta_config
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.indicators import (
    ArtifactSet,
    ShareIndicator,
    TableArtifact,
    ValueIndicator,
    analysis_identity,
    indicator_definition_sha256,
    validate_indicator_definition,
)
from tests.test_campaign_run import meta_fixture
from tests.test_canonical_morris_plan import canonical_meta

FIXTURES = Path(__file__).parent / "fixtures" / "eqasim-output" / "analysis_population"
DISTRIBUTIONS = {
    "age": ("nantes_age.csv", "Age class"),
    "license": ("nantes_license.csv", "Possession of license"),
    "nbr_vehicle": ("nantes_nbr_vehicle.csv", "Number of vehicles class"),
}


def artifacts(**files: str) -> ArtifactSet:
    return ArtifactSet(
        {name: (FIXTURES / file).absolute() for name, file in files.items()}
    )


def share(output: str, category: str) -> ShareIndicator:
    file, column = DISTRIBUTIONS[output]
    return ShareIndicator(
        TableArtifact(output, f"analysis_population/{file}", ",", column, "EQASIM"),
        category,
    )


def value(output: str, column: str, separator: str = ",", **where: str) -> ValueIndicator:
    return ValueIndicator(output, f"{output}.csv", separator, column, where)


def test_share_on_real_csvs() -> None:
    files = {name: file for name, (file, _) in DISTRIBUTIONS.items()}
    available = artifacts(**files)

    assert share("license", "True").compute(available) == pytest.approx(
        1033000.0 / (455000.0 + 1033000.0)
    )
    assert share("nbr_vehicle", "3+").compute(available) == pytest.approx(
        2000.0 / (333000.0 + 226000.0 + 45000.0 + 2000.0)
    )
    assert share("age", "0-10").compute(available) == pytest.approx(
        168000.0 / (168000.0 + 68000.0 + 61000.0 + 143000.0)
    )


def test_value_with_where_on_crosstab() -> None:
    available = artifacts(age_purpose="nantes_age_purpose.csv")

    indicator = value("age_purpose", "Home<>Work", age_class="18-24")

    assert indicator.compute(available) == 63000.0


def test_value_where_on_integer_like_labels() -> None:
    available = artifacts(nbr_vehicle="nantes_nbr_vehicle.csv")

    indicator = value("nbr_vehicle", "EQASIM", **{"Number of vehicles class": "0"})

    assert indicator.compute(available) == 333000.0


def test_value_without_where_requires_single_row(tmp_path: Path) -> None:
    single = tmp_path / "single.csv"
    single.write_text("total;label\n12.5;all\n")
    several = tmp_path / "several.csv"
    several.write_text("total\n1\n2\n")
    available = ArtifactSet({"single": single, "several": several})

    assert value("single", "total", ";").compute(available) == 12.5
    with pytest.raises(ValueError) as caught:
        value("several", "total").compute(available)
    assert str(caught.value) == (
        "value: output 'several' has several rows; add where to select one"
    )


def test_value_error_messages(tmp_path: Path) -> None:
    table = tmp_path / "table.csv"
    table.write_text("k,a,text\nx,1,n/a\nx,2,inf\ny,3,4\n")
    available = ArtifactSet({"o": table})

    with pytest.raises(ValueError) as caught:
        value("o", "a", k="z").compute(available)
    assert str(caught.value) == "value: no row of output 'o' matches where {'k': 'z'}"
    with pytest.raises(ValueError) as caught:
        value("o", "a", k="x").compute(available)
    assert str(caught.value) == (
        "value: several rows of output 'o' match; add where to select one"
    )
    assert value("o", "text", k="y").compute(available) == 4.0
    for where in ({"a": "1"}, {"a": "2"}):
        with pytest.raises(ValueError) as caught:
            value("o", "text", **where).compute(available)
        assert str(caught.value) == "value: 'text' in output 'o' is not a finite number"
    with pytest.raises(ValueError) as caught:
        share("license", "Maybe").compute(artifacts(license="nantes_license.csv"))
    assert str(caught.value) == "share: category 'Maybe' not found in output 'license'"


def _meta_with(tmp_path: Path, outputs: dict, indicators: list) -> Path:
    meta, _, _, _ = meta_fixture(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document["outputs"] = outputs
    document["indicators"] = indicators
    meta.write_text(yaml.safe_dump(document, sort_keys=False))
    return meta


def test_share_and_value_load_from_meta_config(tmp_path: Path) -> None:
    meta = _meta_with(
        tmp_path,
        {
            "license": {"category": "Possession of license", "simulated": "EQASIM"},
            "age_purpose": {},
        },
        [
            {"share": {"output": "license", "category": "True"}},
            {
                "value": {
                    "output": "age_purpose",
                    "column": "Home<>Work",
                    "where": {"age_class": "18-24"},
                }
            },
        ],
    )

    loaded = load_meta_config(meta)

    assert [item.name for item in loaded.indicators] == [
        "share(license, True)",
        "value(age_purpose, Home<>Work, age_class=18-24)",
    ]
    for item in loaded.indicators:
        definition = analysis_identity(item)
        assert validate_indicator_definition(definition, require_scientific=True)
        indicator_definition_sha256(definition)


@pytest.mark.parametrize(
    "outputs,indicator,message",
    [
        (
            {"license": {"category": "Possession of license", "simulated": "EQASIM"}},
            {"share": {"output": "license", "category": True}},
            'share category must be a quoted string (e.g. "0", "True")',
        ),
        (
            {"nbr_vehicle": {"category": "Number of vehicles class", "simulated": "EQASIM"}},
            {"share": {"output": "nbr_vehicle", "category": 0}},
            'share category must be a quoted string (e.g. "0", "True")',
        ),
        (
            {"nbr_vehicle": {}},
            {
                "value": {
                    "output": "nbr_vehicle",
                    "column": "EQASIM",
                    "where": {"Number of vehicles class": 0},
                }
            },
            "where values must be quoted strings",
        ),
    ],
)
def test_unquoted_category_and_where_values_are_rejected(
    tmp_path: Path, outputs: dict, indicator: dict, message: str
) -> None:
    meta = _meta_with(tmp_path, outputs, [indicator])

    with pytest.raises(ValueError) as caught:
        load_meta_config(meta)

    assert str(caught.value) == message


def test_share_requires_category_and_simulated(tmp_path: Path) -> None:
    meta = _meta_with(
        tmp_path,
        {"license": {}},
        [{"share": {"output": "license", "category": "True"}}],
    )

    with pytest.raises(ValueError, match="share.*category and simulated"):
        load_meta_config(meta)


@pytest.mark.parametrize(
    "indicator,primary",
    [
        ({"share": {"output": "mine", "category": "a"}}, "share(mine, a)"),
        (
            {"value": {"output": "mine", "column": "n", "where": {"label": "a"}}},
            "value(mine, n, label=a)",
        ),
    ],
)
def test_scientific_primary_indicator_can_be_share_or_value_on_user_output(
    tmp_path: Path, indicator: dict, primary: str
) -> None:
    meta = canonical_meta(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document["outputs"] = {
        "mine": {"file": "my/{prefix}table.csv", "category": "label", "simulated": "n"}
    }
    document["indicators"] = [indicator]
    document["charter"]["decision"]["primary_indicator"] = primary
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    loaded = load_meta_config(meta)

    assert loaded.charter.decision.primary_indicator == primary
    definition = loaded.charter.decision.primary_indicator_definition
    assert definition is not None and definition["name"] == primary
    plan_from_meta_config(meta)


def test_indicator_names() -> None:
    assert share("age", "65+").name == "share(age, 65+)"
    assert (
        value("age_purpose", "Home<>Work", age_class="18-24").name
        == "value(age_purpose, Home<>Work, age_class=18-24)"
    )
    assert value("o", "k").name == "value(o, k)"
    assert value("o", "k", b="y", a="x").name == "value(o, k, a=x, b=y)"


def test_custom_is_rejected(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document["outputs"] = {
        "age": {"category": "c", "simulated": "s", "references": ["r"]}
    }
    document["indicators"] = [{"custom": {"function": "extension", "output": "age"}}]
    meta.write_text(yaml.safe_dump(document, sort_keys=False))

    with pytest.raises(ValueError) as caught:
        load_meta_config(meta)

    assert str(caught.value) == (
        "indicator 0 must contain exactly one key: one of tv_distance, share, value"
    )
