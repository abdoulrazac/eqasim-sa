"""Indicator kinds accepted by the meta-config."""

from pathlib import Path

import pytest
import yaml

from eqasim_sa.adapters.meta_config import load_meta_config
from tests.test_campaign_run import meta_fixture


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
        "indicator 0 must contain exactly one key: one of tv_distance"
    )
