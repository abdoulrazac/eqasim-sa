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


def test_show_config_names_a_missing_meta_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run_show(monkeypatch, Path("configs/absent.yml")) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == "meta-config not found: configs/absent.yml"
    assert "Errno" not in captured.err


def test_show_config_names_a_missing_model_config(tmp_path, capsys, monkeypatch):
    meta, *_ = meta_fixture(tmp_path)
    meta.write_text(meta.read_text().replace("config: config.yml", "config: ../config.yml"))
    assert run_show(monkeypatch, meta) == 1
    assert capsys.readouterr().err.strip() == (
        "model.config not found: ../config.yml (a relative path is read from model.root)"
    )


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        (
            "root: model",
            "root: absent",
            "model.root not found: absent (a relative path is read from the meta-config folder)",
        ),
        (
            "data_manifest: manifest.json",
            "data_manifest: absent.json",
            "model.data_manifest not found: absent.json "
            "(a relative path is read from the meta-config folder)",
        ),
        (
            "lockfile: model/lock.yml",
            "lockfile: model/absent.yml",
            "model.lockfile not found: model/absent.yml "
            "(a relative path is read from the meta-config folder)",
        ),
    ],
)
def test_meta_config_names_each_missing_file(tmp_path, before, after, message):
    meta, *_ = meta_fixture(tmp_path)
    meta.write_text(meta.read_text().replace(before, after))
    with pytest.raises(FileNotFoundError) as caught:
        load_meta_config(meta, verify_data=False)
    assert str(caught.value) == message


def test_meta_config_names_a_missing_calibration_meta_config(tmp_path):
    meta, *_ = meta_fixture(tmp_path)
    meta.write_text(meta.read_text() + "calibration:\n  meta_configs: [absent.yml]\n")
    with pytest.raises(FileNotFoundError) as caught:
        load_meta_config(meta, verify_data=False)
    assert str(caught.value) == (
        "calibration meta-config not found: absent.yml "
        "(a relative path is read from the meta-config folder)"
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            "campaign: [demo,\n",
            "cannot parse meta-config {path}: ParserError at line 2, column 1: "
            "expected the node content, but found '<stream end>'",
        ),
        (
            "campaign: demo\n  model: x\n",
            "cannot parse meta-config {path}: ScannerError at line 2, column 8: "
            "mapping values are not allowed here",
        ),
    ],
)
def test_meta_config_parse_error_gives_the_cause(tmp_path, capsys, monkeypatch, content, message):
    monkeypatch.chdir(tmp_path)
    Path("broken.yml").write_text(content)
    assert run_show(monkeypatch, Path("broken.yml")) == 1
    assert capsys.readouterr().err.strip() == message.format(path="broken.yml")


def test_meta_config_names_an_unparsable_model_config(tmp_path):
    meta, _, config, _ = meta_fixture(tmp_path)
    config.write_text("config: [a,\n")
    with pytest.raises(ValueError) as caught:
        load_meta_config(meta, verify_data=False)
    assert str(caught.value) == (
        "cannot parse model.config config.yml: ParserError at line 2, column 1: "
        "expected the node content, but found '<stream end>'"
    )


@pytest.mark.parametrize(
    ("command", "stream"),
    [
        (["plan"], "out"),
        (["run"], "out"),
        (["estimate"], "out"),
        (["check"], "out"),
        (["analyze"], "out"),
        (["plot"], "err"),
    ],
)
def test_every_meta_config_command_names_a_missing_meta_config(
    tmp_path, capsys, monkeypatch, command, stream
):
    from eqasim_sa.adapters.application import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["eqasim-sa", *command, "--meta-config", "configs/absent.yml"]
    )
    assert main() == 1
    output = getattr(capsys.readouterr(), stream)
    assert "meta-config not found: configs/absent.yml" in output
    assert "Errno" not in output
