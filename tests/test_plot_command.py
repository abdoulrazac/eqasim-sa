import hashlib
import json
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from eqasim_sa.cli import dispatch
from eqasim_sa.core.analyze import analyze
from eqasim_sa.core.ledger import RunRecord
from eqasim_sa.core.figures import plot as plot_module
from eqasim_sa.core.figures.catalogue import FIGURES
from eqasim_sa.core.figures.common import FigureNotApplicable
from eqasim_sa.core.figures.plot import RENDERERS, plot_campaign
from eqasim_sa.core.figures.published import load_published_analysis
from eqasim_sa.utils.filesystem import write_parquet_atomic
from tests.test_analyze import _age_indicator
from tests.provenance_helpers import GRID_CONTEXT_HASH, grid_plan_table, write_csv
from tests.test_analyze import _write_verified_run

pytestmark = pytest.mark.usefixtures("synthetic_indicator")


def _published(tmp_path: Path) -> Path:
    """A two-run grid over one numeric parameter ``x`` (replication seed role)."""
    root = tmp_path / "campaign"
    run_ids = ("1" * 32, "2" * 32)
    write_parquet_atomic(
        grid_plan_table(
            {
                "designId": ["a" * 32, "b" * 32],
                "method": ["grid"] * 2,
                "role": ["point"] * 2,
                "runId": list(run_ids),
                "seed": [1, 1],
                "contextHash": [GRID_CONTEXT_HASH] * 2,
                "x": [1.0, 2.0],
            }
        ),
        root / "plan.parquet",
    )
    for run_id, value, x in zip(run_ids, (1.0, 5.0), (1.0, 2.0), strict=True):
        write_csv(
            pa.table({"synthetic": [value]}), root / "archives" / run_id / "age.csv"
        )
        _write_verified_run(
            root,
            RunRecord(run_id, "test", 1, GRID_CONTEXT_HASH, "node", 1.0, {"x": x}),
        )
    analyze(root, (_age_indicator(),), campaign="test", completeness_threshold=1.0)
    return root


def _inventory(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _names(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def test_plot_grid_writes_only_valid_figures(tmp_path: Path) -> None:
    root = _published(tmp_path)

    result = plot_campaign(root, ())

    directory = Path(result["directory"])
    assert directory.parent == root.resolve() / "figures"
    assert _names(directory) == {
        "output-summary.png",
        "grid-response-tv_distance_age__ref_reference_.png",
        "figures.json",
    }
    index = json.loads((directory / "figures.json").read_text())
    published = load_published_analysis(root)
    assert index["generationId"] == published.generation_id
    assert index["method"] == "grid"
    assert index["inferenceStatus"] == "demonstration_noninferential"
    assert {entry["file"] for entry in index["figures"]} == {
        "output-summary.png",
        "grid-response-tv_distance_age__ref_reference_.png",
    }
    for entry in index["figures"]:
        for source in entry["sources"]:
            assert source["sha256"] == published.source_sha256(source["role"])
    assert (directory / "output-summary.png").read_bytes().startswith(b"\x89PNG")
    assert [entry["name"] for entry in result["figures"]] == [
        entry["name"] for entry in index["figures"]
    ]


def test_generation_directory_is_untouched(tmp_path: Path) -> None:
    root = _published(tmp_path)
    generations = root / "analysis" / "generations"
    before = _inventory(generations)
    manifest = (root / "analysis-generation.json").read_bytes()

    plot_campaign(root, ())

    assert _inventory(generations) == before
    assert (root / "analysis-generation.json").read_bytes() == manifest
    assert before


def test_invalid_figure_request_fails_before_writing(tmp_path: Path) -> None:
    root = _published(tmp_path)

    with pytest.raises(ValueError, match="morris-plane.*valid: .*output-summary"):
        plot_campaign(root, ("morris-plane",))

    assert not (root / "figures").exists()


def test_missing_analysis_says_run_analyze(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run `eqasim-sa analyze` first"):
        plot_campaign(tmp_path, ())


def test_tampered_table_draws_nothing(tmp_path: Path) -> None:
    root = _published(tmp_path)
    path = load_published_analysis(root).outputs["outputSummary"]
    path.write_bytes(path.read_bytes() + b"x")

    with pytest.raises(ValueError, match="outputSummary.*sha256"):
        plot_campaign(root, ())

    assert not (root / "figures").exists()


def test_new_generation_gets_new_directory(tmp_path: Path) -> None:
    root = _published(tmp_path)
    first = plot_campaign(root, ())
    first_inventory = _inventory(Path(first["directory"]))
    analyze(root, (_age_indicator(),), campaign="test", completeness_threshold=0.5)

    second = plot_campaign(root, ())

    assert second["generationId"] != first["generationId"]
    assert second["directory"] != first["directory"]
    assert {path.name for path in (root / "figures").iterdir()} == {
        first["generationId"],
        second["generationId"],
    }
    assert _inventory(Path(first["directory"])) == first_inventory


def test_rerun_with_narrower_selection_leaves_no_stale_png(tmp_path: Path) -> None:
    root = _published(tmp_path)
    plot_campaign(root, ())

    result = plot_campaign(root, ("output-summary",))

    directory = Path(result["directory"])
    assert _names(directory) == {"output-summary.png", "figures.json"}
    assert _names(directory.parent) == {directory.name}


def test_render_failure_writes_nothing_and_keeps_previous(tmp_path: Path, monkeypatch) -> None:
    root = _published(tmp_path)
    first = plot_campaign(root, ())
    before = _inventory(Path(first["directory"]))

    def boom(analysis):
        raise RuntimeError("boom")

    monkeypatch.setitem(RENDERERS, "grid-response", boom)
    with pytest.raises(RuntimeError, match="boom"):
        plot_campaign(root, ())

    assert _inventory(Path(first["directory"])) == before
    assert _names(Path(first["directory"]).parent) == {first["generationId"]}


def _failing_rename(monkeypatch, failing_calls: set[int]) -> list[tuple[Path, Path]]:
    real = plot_module.rename_with_retry
    calls: list[tuple[Path, Path]] = []

    def rename(source: Path, destination: Path) -> None:
        calls.append((source, destination))
        if len(calls) in failing_calls:
            raise PermissionError(f"rename {len(calls)} refused")
        real(source, destination)

    monkeypatch.setattr(plot_module, "rename_with_retry", rename)
    return calls


def test_failed_swap_restores_previous_figures(tmp_path: Path, monkeypatch) -> None:
    root = _published(tmp_path)
    first = plot_campaign(root, ())
    directory = Path(first["directory"])
    before = _inventory(directory)
    calls = _failing_rename(monkeypatch, {2})

    with pytest.raises(PermissionError, match="rename 2 refused"):
        plot_campaign(root, ())

    assert len(calls) == 3  # retire, publish (fails), roll back
    assert calls[2] == (calls[0][1], directory)
    assert _inventory(directory) == before
    assert _names(directory.parent) == {directory.name}


def test_failed_rollback_keeps_the_original_error(tmp_path: Path, monkeypatch) -> None:
    root = _published(tmp_path)
    plot_campaign(root, ())
    _failing_rename(monkeypatch, {2, 3})

    with pytest.raises(PermissionError, match="rename 2 refused"):
        plot_campaign(root, ())


def test_retired_cleanup_failure_is_best_effort(tmp_path: Path, monkeypatch) -> None:
    root = _published(tmp_path)
    plot_campaign(root, ())

    def refuse(path: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(plot_module, "remove_tree", refuse)
    result = plot_campaign(root, ())

    directory = Path(result["directory"])
    assert _names(directory) >= {"output-summary.png", "figures.json"}


def test_not_applicable_is_skipped_in_auto_and_fatal_when_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    root = _published(tmp_path)

    def nope(analysis):
        raise FigureNotApplicable("no numeric parameter")

    monkeypatch.setitem(RENDERERS, "grid-response", nope)

    result = plot_campaign(root, ())
    assert result["skipped"] == [{"name": "grid-response", "reason": "no numeric parameter"}]
    directory = Path(result["directory"])
    assert _names(directory) == {"output-summary.png", "figures.json"}
    index = json.loads((directory / "figures.json").read_text())
    assert index["skipped"] == result["skipped"]

    with pytest.raises(FigureNotApplicable, match="no numeric parameter"):
        plot_campaign(root, ("grid-response",))
    assert _names(directory) == {"output-summary.png", "figures.json"}


def test_indicator_names_are_sanitised_and_collisions_fail(
    tmp_path: Path, monkeypatch
) -> None:
    root = _published(tmp_path)
    real = RENDERERS["output-summary"]

    def renamed(analysis):
        item = real(analysis)[0]
        return [item.__class__(item.name, "a/b c", item.figure, item.sources, item.omitted)]

    monkeypatch.setitem(RENDERERS, "output-summary", renamed)
    result = plot_campaign(root, ("output-summary",))
    assert [entry["file"] for entry in result["figures"]] == ["output-summary-a_b_c.png"]

    def clashing(analysis):
        item = real(analysis)[0]
        return [
            item.__class__(item.name, "a/b", item.figure, item.sources, item.omitted),
            item.__class__(item.name, "a b", item.figure, item.sources, item.omitted),
        ]

    monkeypatch.setitem(RENDERERS, "output-summary", clashing)
    before = _inventory(root / "figures")
    with pytest.raises(ValueError, match="output-summary-a_b.png"):
        plot_campaign(root, ("output-summary",))
    assert _inventory(root / "figures") == before


def test_renderers_cover_catalogue() -> None:
    assert set(RENDERERS) == {spec.name for spec in FIGURES}
    assert plot_module.RENDERERS is RENDERERS


def _run(monkeypatch, *arguments: str, factory) -> int:
    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "plot", *arguments])
    return dispatch(object(), object(), plot_factory=factory)


def test_cli_plot_prints_json(tmp_path, monkeypatch, capsys) -> None:
    calls = []

    def factory(path, figures):
        calls.append((path, figures))
        return {"z": 1, "status": "completed", "a": [1]}

    code = _run(
        monkeypatch,
        "--meta-config",
        str(tmp_path / "m.yml"),
        "--figure",
        "output-summary",
        "--figure",
        "grid-response",
        factory=factory,
    )

    assert code == 0
    assert capsys.readouterr().out == '{"a": [1], "status": "completed", "z": 1}\n'
    assert calls == [(tmp_path / "m.yml", ("output-summary", "grid-response"))]


def test_cli_plot_without_figure_passes_empty_selection(tmp_path, monkeypatch) -> None:
    calls = []
    code = _run(
        monkeypatch,
        "--meta-config",
        str(tmp_path / "m.yml"),
        factory=lambda path, figures: calls.append(figures) or {},
    )
    assert code == 0
    assert calls == [()]


def test_cli_plot_error_goes_to_stderr(tmp_path, monkeypatch, capsys) -> None:
    def factory(path, figures):
        raise ValueError("figure 'x' is not valid; valid: a, b")

    code = _run(monkeypatch, "--meta-config", str(tmp_path / "m.yml"), factory=factory)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "figure 'x' is not valid; valid: a, b" in captured.err
