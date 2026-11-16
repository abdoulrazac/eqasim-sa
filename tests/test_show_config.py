import sys
from pathlib import Path

import pytest
import yaml

from eqasim_sa.adapters.application import show_config_from_meta_config
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.cli import dispatch
from tests.test_campaign_run import meta_fixture


def run_show(monkeypatch: pytest.MonkeyPatch, meta: Path) -> int:
    monkeypatch.setattr(sys, "argv", ["eqasim-sa", "show-config", str(meta)])
    return dispatch(
        object(), object(), show_config_factory=show_config_from_meta_config
    )


def test_show_config_prints_resolved_document(tmp_path, capsys, monkeypatch):
    meta, *_ = meta_fixture(tmp_path)
    assert run_show(monkeypatch, meta) == 0
    shown = yaml.safe_load(capsys.readouterr().out)
    assert shown["analysis"] == {"completeness_threshold": 1.0}
    assert shown["model"]["lockfile"] == "model/lock.yml"
    assert shown["campaign_root"] == "campaign"


def test_show_config_output_is_a_valid_input(tmp_path, capsys, monkeypatch):
    meta, *_ = meta_fixture(tmp_path)
    assert run_show(monkeypatch, meta) == 0
    again = tmp_path / "again.yml"
    again.write_text(capsys.readouterr().out)
    first = load_meta_config(meta, verify_data=False)
    second = load_meta_config(again, verify_data=False)
    assert second.seeds == first.seeds
    assert second.factors == first.factors
    assert second.indicators == first.indicators


def test_show_config_reports_invalid_file(tmp_path, capsys, monkeypatch):
    meta, *_ = meta_fixture(tmp_path)
    meta.write_text(meta.read_text().replace("seeds: [41, 42]", "seeds: [41, 42]\n  seed_role: factor"))
    assert run_show(monkeypatch, meta) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "experiment keys" in captured.err
