from pathlib import Path

import pytest

from eqasim_sa.utils import names
from tests.test_plan import meta_fixture
from eqasim_sa.adapters.meta_config import load_meta_config


@pytest.mark.parametrize(
    "name",
    ["nul", "CON", "Aux.txt", "nul.txt", "com1", "LPT9.parquet", "pilot.", "pilot "],
)
def test_rejects_non_portable(name: str) -> None:
    with pytest.raises(ValueError, match="is not a portable file name"):
        names.validate_portable_name(name, "campaign")


@pytest.mark.parametrize("name", ["nantes-pilot-v1", "console", "com10", "a.b"])
def test_accepts_portable(name: str) -> None:
    assert names.validate_portable_name(name, "campaign") == name


def test_rejects_case_collision() -> None:
    with pytest.raises(ValueError, match="differ only by case"):
        names.reject_case_collisions(["Age", "age"], "artifact")
    names.reject_case_collisions(["age", "age2"], "artifact")


def test_meta_config_rejects_reserved_campaign(tmp_path: Path) -> None:
    meta, _, _, _ = meta_fixture(tmp_path)
    meta.write_text(meta.read_text().replace("campaign: demo", "campaign: nul", 1))
    with pytest.raises(ValueError, match="not a portable file name"):
        load_meta_config(meta)
