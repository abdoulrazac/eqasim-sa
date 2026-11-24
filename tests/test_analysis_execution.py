"""Analysis scripts run in a separate interpreter with a fingerprinted cache."""

import ast
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from eqasim_sa.adapters import analysis_execution
from eqasim_sa.adapters.analysis_execution import (
    AnalysisCache,
    AnalysisResult,
    AnalysisScriptError,
    environment_document,
    environment_fingerprint,
    result_fingerprint,
    run_analysis,
)
from eqasim_sa.core.analyses import (
    CACHE_ENTRY,
    CACHE_TEMPORARY,
    LOG_ENTRY,
    AnalysisDeclaration,
)
from eqasim_sa.core.indicators import DerivedOutput, read_csv_table

FIXTURES = Path(__file__).parent / "fixtures" / "analyses"
RUNNER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "eqasim_sa"
    / "adapters"
    / "analysis_runner.py"
)
RUN_ID = "0123456789abcdef0123456789abcdef"
ENVIRONMENT = "e" * 64
PYTHON = (sys.executable,)


def _declaration(name: str, script: Path, inputs=("trips",)) -> AnalysisDeclaration:
    inputs = tuple(inputs)
    return AnalysisDeclaration(
        name,
        script.resolve(),
        inputs,
        DerivedOutput(name, name, inputs),
    )


def _campaign(tmp_path: Path) -> tuple[Path, Path, Path]:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    model = tmp_path / "model"
    model.mkdir()
    archive = tmp_path / "archive" / "trips.csv"
    archive.parent.mkdir()
    archive.write_text("person,distance\n1,2.0\n2,4.0\n", encoding="utf-8")
    return campaign, model, archive


def _parameters(**factors) -> dict:
    return {"runId": RUN_ID, "seed": 7, "factors": factors}


def _run(
    declaration, archive, campaign, model, parameters=None, **options
) -> AnalysisResult:
    return run_analysis(
        declaration,
        {"trips": archive},
        _parameters() if parameters is None else parameters,
        PYTHON,
        model,
        campaign,
        ENVIRONMENT,
        **options,
    )


def test_runner_calls_compute_and_writes_csv(tmp_path):
    campaign, model, archive = _campaign(tmp_path)
    declaration = _declaration("mean_distance", FIXTURES / "mean_distance.py")

    record = _run(declaration, archive, campaign, model)
    result = record.path

    table = read_csv_table(result, ",", "mean_distance")
    assert table.column_names == ["runId", "seed", "mean_distance"]
    assert table.to_pylist() == [
        {"runId": RUN_ID, "seed": "7", "mean_distance": "3.0"}
    ]
    assert result.parent == campaign / "analyses-cache" / "mean_distance"
    assert CACHE_ENTRY.fullmatch(result.name)
    digest = result.with_suffix(".sha256").read_text(encoding="utf-8").strip()
    assert digest == hashlib.sha256(result.read_bytes()).hexdigest()
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert record.fingerprint == result.stem == result_fingerprint(
        declaration, {"trips": archive_sha256}, _parameters(), ENVIRONMENT
    )
    assert record.script_sha256 == hashlib.sha256(
        (FIXTURES / "mean_distance.py").read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()
    assert record.inputs_sha256 == {"trips": archive_sha256}
    assert record.environment == ENVIRONMENT
    assert record.warnings == ()


def test_runner_reports_exception_with_script_and_run(tmp_path):
    campaign, model, archive = _campaign(tmp_path)
    declaration = _declaration("failing", FIXTURES / "failing.py")

    with pytest.raises(AnalysisScriptError) as caught:
        _run(declaration, archive, campaign, model)

    assert str(caught.value) == (
        f"analysis 'failing' failed for run {RUN_ID}: "
        "ValueError: distance négative"
    )
    log = campaign / "analyses-cache" / "logs" / f"failing-{RUN_ID}.log"
    assert LOG_ENTRY.fullmatch(log.name)
    text = log.read_text(encoding="utf-8")
    assert "Traceback" in text
    assert "ValueError: distance négative" in text
    assert "début du calcul" in text
    assert sorted(path.name for path in log.parent.iterdir()) == [log.name]
    assert not (campaign / "analyses-cache" / "failing").exists() or not any(
        (campaign / "analyses-cache" / "failing").iterdir()
    )


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("non_dataframe.py", "compute must return a pandas.DataFrame"),
        ("duplicate_columns.py", "duplicate column name"),
        ("no_columns.py", "at least one column"),
    ],
)
def test_runner_rejects_non_dataframe_and_duplicate_columns(tmp_path, script, message):
    campaign, model, archive = _campaign(tmp_path)
    name = script.removesuffix(".py")
    declaration = _declaration(name, FIXTURES / script)

    with pytest.raises(AnalysisScriptError) as caught:
        _run(declaration, archive, campaign, model)

    assert str(caught.value).startswith(f"analysis '{name}' failed for run {RUN_ID}: ")
    assert message in str(caught.value)


def test_script_receives_copies_not_archives(tmp_path):
    campaign, model, archive = _campaign(tmp_path)
    original = archive.read_bytes()
    declaration = _declaration("writes_input", FIXTURES / "writes_input.py")

    result = _run(declaration, archive, campaign, model).path

    assert archive.read_bytes() == original
    row = read_csv_table(result, ",", "writes_input").to_pylist()[0]
    assert row["name"] == "trips.csv"
    assert Path(row["path"]) != archive
    assert not Path(row["path"]).exists()


def test_runner_imports_only_stdlib_and_pandas_and_parses_as_py39():
    source = RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"pandas"}
    assert imported <= allowed, imported - allowed
    assert "eqasim_sa" not in source
    assert "lineterminator" not in source
    assert "line_terminator" not in source


def test_runner_is_copied_so_eqasim_sa_adapters_is_not_on_sys_path(tmp_path):
    campaign, model, archive = _campaign(tmp_path)
    declaration = _declaration("sys_path", FIXTURES / "sys_path.py")

    result = _run(declaration, archive, campaign, model).path

    entries = [row["entry"] for row in read_csv_table(result, ",", "x").to_pylist()]
    adapters = RUNNER.parent
    assert all(
        not entry or Path(entry).resolve() != adapters for entry in entries
    )
    assert all(not entry or Path(entry).resolve() != FIXTURES for entry in entries)


def test_script_copy_is_hashed_not_the_user_file(tmp_path, monkeypatch):
    campaign, model, archive = _campaign(tmp_path)
    script = tmp_path / "scripts" / "edited.py"
    script.parent.mkdir()
    first = (FIXTURES / "mean_distance.py").read_text(encoding="utf-8")
    script.write_text(first, encoding="utf-8")
    declaration = _declaration("edited", script)
    captured = analysis_execution.capture_stable_file

    def capture_then_edit(source, destination, **options):
        snapshot = captured(source, destination, **options)
        if Path(source) == script.resolve():
            script.write_text("raise SystemExit('edited')\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(analysis_execution, "capture_stable_file", capture_then_edit)

    record = _run(declaration, archive, campaign, model)
    result = record.path

    original = tmp_path / "original.py"
    original.write_text(first, encoding="utf-8")
    expected = result_fingerprint(
        _declaration("edited", original),
        {"trips": hashlib.sha256(archive.read_bytes()).hexdigest()},
        _parameters(),
        ENVIRONMENT,
    )
    assert result.name == f"{expected}.csv"
    assert record.fingerprint == expected
    assert record.script_sha256 == hashlib.sha256(first.encode("utf-8")).hexdigest()
    assert read_csv_table(result, ",", "edited").num_rows == 1

    # A cache hit hashes this call's copy too, not the file read afterwards.
    script.write_text(first, encoding="utf-8")
    hit = _run(declaration, archive, campaign, model)
    assert hit.path == result
    assert hit.script_sha256 == record.script_sha256


def _wrapper(tmp_path: Path, body: str) -> tuple[str, ...]:
    wrapper = tmp_path / "wrapper" / "python_wrapper.py"
    wrapper.parent.mkdir(exist_ok=True)
    wrapper.write_text(
        "import runpy\nimport sys\nimport types\n"
        + body
        + "\nsys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n",
        encoding="utf-8",
    )
    return (sys.executable, str(wrapper))


def _installed(name: str):
    try:
        return __import__(name).__version__
    except ImportError:
        return None


def _expected_environment(versions: dict, lockfile_sha256) -> str:
    document = {**versions, "lockfileSha256": lockfile_sha256}
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_probe_reports_null_for_missing_pyarrow(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    python = _wrapper(tmp_path, "sys.modules['pyarrow'] = None")

    fingerprint = environment_fingerprint(python, model, model / "uv.lock")

    import numpy
    import pandas

    assert fingerprint == _expected_environment(
        {
            "python": sys.version,
            "pandas": pandas.__version__,
            "numpy": numpy.__version__,
            "pyarrow": None,
            "geopandas": _installed("geopandas"),
            "shapely": _installed("shapely"),
            "pyogrio": _installed("pyogrio"),
        },
        None,
    )


def test_environment_fingerprint_uses_reported_versions(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    lockfile = model / "uv.lock"
    lockfile.write_bytes(b"version = 1\r\n")
    python = _wrapper(
        tmp_path,
        "for name in ('pandas', 'numpy', 'pyarrow', 'geopandas', 'shapely'):\n"
        "    module = types.ModuleType(name)\n"
        "    module.__version__ = name + '-reported'\n"
        "    sys.modules[name] = module",
    )

    fingerprint = environment_fingerprint(python, model, lockfile)

    assert fingerprint == _expected_environment(
        {
            "python": sys.version,
            "pandas": "pandas-reported",
            "numpy": "numpy-reported",
            "pyarrow": "pyarrow-reported",
            "geopandas": "geopandas-reported",
            "shapely": "shapely-reported",
            "pyogrio": _installed("pyogrio"),
        },
        hashlib.sha256(b"version = 1\n").hexdigest(),
    )
    lockfile.write_bytes(b"version = 2\n")
    assert environment_fingerprint(python, model, lockfile) != fingerprint


def test_result_fingerprint_changes_with_script_inputs_parameters_environment(tmp_path):
    first = tmp_path / "first.py"
    first.write_text("def compute(files, parameters):\n    pass\n", encoding="utf-8")
    crlf = tmp_path / "crlf.py"
    crlf.write_bytes(b"def compute(files, parameters):\r\n    pass\r\n")
    second = tmp_path / "second.py"
    second.write_text("def compute(files, parameters):\n    return 1\n", encoding="utf-8")
    inputs = {"trips": "a" * 64}
    base = result_fingerprint(_declaration("x", first), inputs, _parameters(), ENVIRONMENT)

    assert len(base) == 64
    assert result_fingerprint(_declaration("x", crlf), inputs, _parameters(), ENVIRONMENT) == base
    variants = {
        result_fingerprint(_declaration("x", second), inputs, _parameters(), ENVIRONMENT),
        result_fingerprint(_declaration("y", first), inputs, _parameters(), ENVIRONMENT),
        result_fingerprint(_declaration("x", first), {"trips": "b" * 64}, _parameters(), ENVIRONMENT),
        result_fingerprint(_declaration("x", first), inputs, _parameters(alpha=1), ENVIRONMENT),
        result_fingerprint(_declaration("x", first), inputs, _parameters(), "f" * 64),
    }
    assert base not in variants
    assert len(variants) == 5
    document = {
        "analysis": "x",
        "script": hashlib.sha256(first.read_bytes()).hexdigest(),
        "inputs": inputs,
        "parameters": _parameters(),
        "environment": ENVIRONMENT,
    }
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert base == hashlib.sha256(b"eqasim-sa:analysis-result:v1\0" + payload).hexdigest()
    with pytest.raises(ValueError):
        result_fingerprint(_declaration("x", first), {}, _parameters(), ENVIRONMENT)


def test_cache_hit_skips_execution(tmp_path):
    campaign, model, archive = _campaign(tmp_path)
    counter = tmp_path / "count.txt"
    declaration = _declaration("counter", FIXTURES / "counter.py")
    parameters = _parameters(counter=str(counter))

    first = _run(declaration, archive, campaign, model, parameters)
    second = _run(declaration, archive, campaign, model, parameters)

    assert (first.cached, second.cached) == (False, True)
    assert replace(first, cached=True) == second
    assert counter.read_text() == "1"
    cache = AnalysisCache(campaign)
    warnings = []
    assert cache.get("counter", first.fingerprint, warnings) == first.path
    assert warnings == []
    assert cache.get("counter", "0" * 64, warnings) is None


def test_tampered_cache_entry_is_recomputed(tmp_path):
    campaign, model, archive = _campaign(tmp_path)
    counter = tmp_path / "count.txt"
    declaration = _declaration("counter", FIXTURES / "counter.py")
    parameters = _parameters(counter=str(counter))
    first = _run(declaration, archive, campaign, model, parameters).path
    original = first.read_bytes()
    os.chmod(first, 0o600)
    first.write_bytes(b"value\n9.5\n")

    record = _run(declaration, archive, campaign, model, parameters)
    again = record.path

    assert again == first
    assert again.read_bytes() == original
    assert counter.read_text() == "2"
    assert record.cached is False
    assert record.warnings == (
        f"analysis cache entry counter/{first.name} was altered; "
        "it was removed and recomputed",
    )
    names = sorted(path.name for path in first.parent.iterdir())
    assert names == sorted([first.name, f"{first.stem}.sha256"])


def test_cache_put_publishes_digest_before_csv_with_recognized_temporaries(
    tmp_path, monkeypatch
):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    source = tmp_path / "result.csv"
    source.write_text("a\n1\n", encoding="utf-8")
    fingerprint = "c" * 64
    seen = []
    replace = analysis_execution.platform.replace_with_retry

    def record(temporary, destination):
        seen.append((Path(temporary).name, Path(destination).name))
        replace(temporary, destination)

    monkeypatch.setattr(analysis_execution.platform, "replace_with_retry", record)

    published = AnalysisCache(campaign).put("result", fingerprint, source)

    assert published == campaign / "analyses-cache" / "result" / f"{fingerprint}.csv"
    assert [destination for _, destination in seen] == [
        f"{fingerprint}.sha256",
        f"{fingerprint}.csv",
    ]
    assert all(CACHE_TEMPORARY.fullmatch(temporary) for temporary, _ in seen)
    assert published.read_bytes() == b"a\n1\n"


def test_log_write_failure_keeps_the_script_error(tmp_path, monkeypatch):
    campaign, model, archive = _campaign(tmp_path)
    declaration = _declaration("failing", FIXTURES / "failing.py")

    def broken_log(*_arguments):
        raise OSError("disk full")

    monkeypatch.setattr(analysis_execution, "_write_log", broken_log)

    with pytest.raises(AnalysisScriptError) as caught:
        _run(declaration, archive, campaign, model)

    assert str(caught.value) == (
        f"analysis 'failing' failed for run {RUN_ID}: "
        "ValueError: distance négative"
    )
    assert isinstance(caught.value.__cause__, OSError)
    assert str(caught.value.__cause__) == "disk full"


def test_environment_document_lists_every_reported_version(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    python = _wrapper(
        tmp_path, "sys.modules['geopandas'] = None\nsys.modules['pyogrio'] = None"
    )

    document = environment_document(python, model, model / "uv.lock")

    assert set(document) == {
        "python",
        "pandas",
        "numpy",
        "pyarrow",
        "geopandas",
        "shapely",
        "pyogrio",
        "lockfileSha256",
    }
    assert document["geopandas"] is None
    assert document["pyogrio"] is None
    assert document["lockfileSha256"] is None
    assert environment_fingerprint(python, model, model / "uv.lock") == (
        hashlib.sha256(
            json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
    )


def test_cleanup_failure_does_not_fail_a_computed_result(tmp_path, monkeypatch):
    campaign, model, archive = _campaign(tmp_path)
    script = tmp_path / "ok.py"
    script.write_text(
        "import pandas\n\ndef compute(files, parameters):\n"
        "    return pandas.DataFrame({'a': [1]})\n",
        encoding="utf-8",
    )

    def locked(*args, **kwargs):
        raise PermissionError("held by an indexer")

    monkeypatch.setattr(analysis_execution.shutil, "rmtree", locked)
    monkeypatch.setattr(analysis_execution.time, "sleep", lambda _seconds: None)

    result = _run(_declaration("ok", script), archive, campaign, model)

    assert read_csv_table(result.path, ",", "ok").num_rows == 1


def test_cache_get_ignores_a_linked_analysis_directory(tmp_path):
    cache = analysis_execution.AnalysisCache(tmp_path)
    cache.root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    fingerprint = "a" * 64
    (target / f"{fingerprint}.csv").write_text("a\n1\n", encoding="utf-8")
    try:
        (cache.root / "x").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable")

    with pytest.raises((OSError, ValueError)):
        cache.get("x", fingerprint)


def test_script_does_not_inherit_the_virtual_env_of_eqasim_sa(tmp_path, monkeypatch):
    # uv warns when VIRTUAL_ENV names another environment than the eqasim one.
    campaign, model, archive = _campaign(tmp_path)
    script = tmp_path / "virtual_env.py"
    script.write_text(
        "import os\nimport pandas\n\n\n"
        "def compute(files, parameters):\n"
        "    return pandas.DataFrame({'runId': [parameters['runId']],\n"
        "        'seed': [parameters['seed']],\n"
        "        'venv': [os.environ.get('VIRTUAL_ENV', 'absent')],\n"
        "        'kept': [os.environ.get('EQASIM_SA_KEPT', 'absent')]})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "eqasim-sa-venv"))
    monkeypatch.setenv("EQASIM_SA_KEPT", "yes")

    result = _run(_declaration("virtual_env", script), archive, campaign, model).path

    row = read_csv_table(result, ",", "virtual_env").to_pylist()[0]
    assert row["venv"] == "absent"
    assert row["kept"] == "yes"


def test_interpreter_probe_does_not_inherit_the_virtual_env(tmp_path, monkeypatch):
    calls = []
    real_run = analysis_execution.subprocess.run

    def spy(*arguments, **options):
        calls.append(options.get("env"))
        return real_run(*arguments, **options)

    model = tmp_path / "model"
    model.mkdir()
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "eqasim-sa-venv"))
    monkeypatch.setattr(analysis_execution.subprocess, "run", spy)

    environment_document(PYTHON, model, None)

    assert calls and all(env is not None and "VIRTUAL_ENV" not in env for env in calls)


def test_parquet_and_geoparquet_inputs_reach_the_script_unchanged(tmp_path):
    campaign, model, _ = _campaign(tmp_path)
    contents = {
        "persons_parquet": ("persons_parquet.parquet", b"PAR1\0persons\r\nPAR1"),
        "homes_spatial": ("homes_spatial.geoparquet", b"PAR1\0homes\nPAR1"),
    }
    archived = {}
    for name, (file, content) in contents.items():
        path = tmp_path / "archive" / file
        path.write_bytes(content)
        archived[name] = path
    script = tmp_path / "inputs.py"
    script.write_text(
        "import hashlib, os, pandas\n"
        "def compute(files, parameters):\n"
        "    names = sorted(files)\n"
        "    return pandas.DataFrame({\n"
        "        'input': names,\n"
        "        'file': [os.path.basename(files[n]) for n in names],\n"
        "        'sha256': [hashlib.sha256(open(files[n], 'rb').read()).hexdigest()\n"
        "                   for n in names],\n"
        "    })\n",
        encoding="utf-8",
    )
    declaration = _declaration("inputs", script, tuple(contents))
    record = run_analysis(
        declaration, archived, _parameters(), PYTHON, model, campaign, ENVIRONMENT
    )
    rows = read_csv_table(record.path, ",", "inputs").to_pylist()
    assert rows == [
        {
            "input": name,
            "file": contents[name][0],
            "sha256": hashlib.sha256(contents[name][1]).hexdigest(),
        }
        for name in sorted(contents)
    ]
