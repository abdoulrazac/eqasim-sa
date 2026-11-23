import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.figures.published import load_published_analysis
from tests.test_analyze import _age_indicator
from tests.test_governed_reporting import _grid_with_duplicate_positions


def _published(tmp_path: Path) -> Path:
    root = _grid_with_duplicate_positions(tmp_path)
    analyze(root, (_age_indicator(),), campaign="test", completeness_threshold=1.0)
    return root


@pytest.mark.usefixtures("synthetic_indicator")
def test_loads_grid_generation(tmp_path: Path) -> None:
    root = _published(tmp_path)

    published = load_published_analysis(root)

    assert published.method == "grid"
    assert published.seed_role == "replication"
    assert published.inference_status == "demonstration_noninferential"
    assert {"outputSummary", "dispersion", "governedReport", "indicators"} <= set(
        published.outputs
    )
    assert published.governed_report["claimAuthorized"] is False
    assert all(path.is_absolute() for path in published.outputs.values())
    assert published.table("outputSummary").num_rows > 0
    with pytest.raises(KeyError, match="noSuchRole"):
        published.table("noSuchRole")


def test_missing_manifest_says_run_analyze(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run `eqasim-sa analyze` first"):
        load_published_analysis(tmp_path)


@pytest.mark.usefixtures("synthetic_indicator")
def test_tampered_table_is_refused(tmp_path: Path) -> None:
    root = _published(tmp_path)
    published = load_published_analysis(root)
    path = published.outputs["outputSummary"]
    table = pq.read_table(path)
    pq.write_table(table.slice(0, max(table.num_rows - 1, 0)), path)

    with pytest.raises(ValueError, match="outputSummary.*sha256"):
        published.table("outputSummary")


@pytest.mark.usefixtures("synthetic_indicator")
def test_tampered_governed_report_is_refused(tmp_path: Path) -> None:
    root = _published(tmp_path)
    path = load_published_analysis(root).outputs["governedReport"]
    path.write_text(path.read_text() + " ")

    with pytest.raises(ValueError, match="governedReport.*sha256"):
        load_published_analysis(root)


@pytest.mark.usefixtures("synthetic_indicator")
@pytest.mark.parametrize(("key", "value"), [("version", 7), ("layout", "mutable-v0")])
def test_wrong_manifest_is_refused(tmp_path: Path, key: str, value: object) -> None:
    root = _published(tmp_path)
    manifest = root / "analysis-generation.json"
    data = json.loads(manifest.read_text())
    data[key] = value
    manifest.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="unsupported analysis manifest"):
        load_published_analysis(root)
