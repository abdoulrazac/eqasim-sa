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
    assert "unknown: seed_role" in captured.err


def test_show_config_writes_utf8_bytes_to_stdout_buffer(tmp_path, monkeypatch):
    import io

    meta, *_ = meta_fixture(tmp_path)
    document = yaml.safe_load(meta.read_text())
    document["outputs"] = {
        "age": {"category": "Classe d'âge", "simulated": "s", "references": ["r"]}
    }
    meta.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    assert run_show(monkeypatch, meta) == 0
    stdout.flush()
    data = raw.getvalue()
    assert "Classe d'âge".encode("utf-8") in data
    again = tmp_path / "again.yml"
    again.write_bytes(data)
    assert load_meta_config(again, verify_data=False).seeds == (41, 42)


def test_show_config_writes_the_analysis_python_resolved_at_load(
    tmp_path, capsys, monkeypatch
):
    from tests.test_campaign_run import add_execution

    meta, *_ = meta_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    add_execution(meta, scratch)
    assert run_show(monkeypatch, meta) == 0
    shown = yaml.safe_load(capsys.readouterr().out)
    loaded = load_meta_config(meta, verify_data=False)
    assert shown["execution"]["python"] == list(loaded.analysis_python) == ["python3"]
    again = tmp_path / "again.yml"
    again.write_text(yaml.safe_dump(shown, sort_keys=False))
    assert load_meta_config(again, verify_data=False).analysis_python == ("python3",)
