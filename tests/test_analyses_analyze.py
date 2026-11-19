"""``analyze`` runs the analysis scripts read by indicators, with a cache."""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from eqasim_sa.adapters import analysis_execution
from eqasim_sa.adapters.analysis_execution import AnalysisCache
from eqasim_sa.adapters import checks
from eqasim_sa.adapters.application import (
    analyze_from_meta_config,
    init_from_inputs,
    plan_from_meta_config,
    run_from_meta_config,
)
from eqasim_sa.adapters.campaign_design import _design
from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.indicators import analysis_identity
from eqasim_sa.core.plan import load_run_specs
from eqasim_sa.utils.filesystem import file_lease
from eqasim_sa.utils.text_identity import lf_sha256
from tests.test_init import initialize, invocation
from tests.test_sealed_analysis import campaign

COMMUTE = """\
import os
import pandas

CALLS = {calls!r}


def compute(files, parameters):
    with open(CALLS, "a", encoding="utf-8") as stream:
        stream.write("{name} " + parameters["runId"] + "\\n")
    {extra}
    trips = pandas.read_csv(files["trips"], sep=";")
    return pandas.DataFrame(
        {{
            "rows": [len(trips)],
            "seed": [parameters["seed"]],
            "rate": [parameters["factors"]["sampling_rate"]],
        }}
    )
"""


@pytest.fixture
def spy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQASIM_SPY_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("EQASIM_SPY_CASE", "complete-references")


def _edit(meta: Path, edit) -> None:  # type: ignore[no-untyped-def]
    document = yaml.safe_load(meta.read_text())
    edit(document)
    meta.write_text(yaml.safe_dump(document, sort_keys=False))


def _write_script(
    base: Path, name: str, extra: str = "pass", calls: Path | None = None
) -> Path:
    folder = base / "analyses"
    folder.mkdir(exist_ok=True)
    script = folder / f"{name}.py"
    calls = calls or base / "calls.txt"
    script.write_text(COMMUTE.format(calls=str(calls), name=name, extra=extra))
    return script


def _project(
    base: Path,
    *,
    seeds: list[int] | None = None,
    analyses: tuple[str, ...] = ("commute",),
    used: tuple[str, ...] | None = None,
    workers: int = 1,
    threshold: float | None = None,
) -> Path:
    meta, _ = campaign(base, seeds=seeds)
    for name in analyses:
        _write_script(base, name)
    used = analyses if used is None else used

    def edit(document: dict) -> None:
        document["outputs"]["trips"] = {"file": "{prefix}trips.csv", "separator": ";"}
        document["analyses"] = {
            name: {"script": f"analyses/{name}.py", "inputs": ["trips"]}
            for name in analyses
        }
        document["indicators"].extend(
            {"value": {"output": name, "column": "rows"}} for name in used
        )
        document["execution"]["python"] = [sys.executable]
        document["execution"]["workers"] = workers
        if threshold is not None:
            document["analysis"] = {"completeness_threshold": threshold}

    _edit(meta, edit)
    return meta


def _calls(base: Path) -> list[str]:
    path = base / "calls.txt"
    return sorted(path.read_text().splitlines()) if path.exists() else []


def _run_ids(base: Path) -> list[str]:
    return sorted(
        set(pq.read_table(base / "campaign" / "plan.parquet")["runId"].to_pylist())
    )


def _values(payload: dict) -> dict[tuple[str, str], float]:
    table = pq.read_table(payload["path"]).to_pylist()
    return {(row["runId"], row["indicator"]): row["value"] for row in table}


def _manifest(base: Path) -> dict:
    return json.loads((base / "campaign" / "analysis-generation.json").read_text())


def test_analyze_computes_value_from_analysis_script(
    tmp_path: Path, spy: None
) -> None:
    meta = _project(tmp_path, seeds=[41, 42])
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 2

    payload = analyze_from_meta_config(meta)

    assert payload["status"] == "completed"
    assert payload["warnings"] == []
    run_ids = _run_ids(tmp_path)
    values = _values(payload)
    for run_id in run_ids:
        assert values[run_id, "value(commute, rows)"] == 2.0
    assert _calls(tmp_path) == sorted(f"commute {run_id}" for run_id in run_ids)
    entries = sorted((tmp_path / "campaign" / "analyses-cache" / "commute").iterdir())
    assert sorted(entry.suffix for entry in entries) == [
        ".csv",
        ".csv",
        ".sha256",
        ".sha256",
    ]
    seeds = set()
    for entry in entries:
        if entry.suffix == ".csv":
            row = entry.read_text().splitlines()[1].split(",")
            seeds.add(int(row[1]))
            assert float(row[2]) == 0.001
    assert seeds == {41, 42}
    # The cache is reused: nothing runs again when nothing changed.
    assert analyze_from_meta_config(meta)["status"] == "completed"
    assert len(_calls(tmp_path)) == 2


def test_analyze_after_copy_without_scratch_and_eqasim_changed(
    tmp_path: Path, spy: None
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    meta = _project(origin)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 1
    moved = tmp_path / "moved"
    shutil.copytree(origin, moved, symlinks=True)
    shutil.rmtree(origin)
    shutil.rmtree(moved / "scratch")
    shutil.rmtree(moved / "data")
    (moved / "model" / "changed.py").write_text("x = 1\n")
    (moved / "model" / "uv.lock").write_text("relocked\n")
    _write_script(moved, "commute", calls=moved / "calls.txt")

    payload = analyze_from_meta_config(moved / "meta.yml")

    assert payload["status"] == "completed"
    [run_id] = _run_ids(moved)
    assert _values(payload)[run_id, "value(commute, rows)"] == 2.0
    assert _calls(moved) == [f"commute {run_id}"]


def test_editing_script_recomputes_only_its_results(tmp_path: Path, spy: None) -> None:
    meta = _project(tmp_path, seeds=[41, 42], analyses=("commute", "other"))
    plan_from_meta_config(meta)
    run_from_meta_config(meta)
    analyze_from_meta_config(meta)
    run_ids = _run_ids(tmp_path)
    assert len(_calls(tmp_path)) == 4
    (tmp_path / "calls.txt").unlink()
    script = tmp_path / "analyses" / "commute.py"
    script.write_text(script.read_text() + "\n# edited\n")

    analyze_from_meta_config(meta)

    assert _calls(tmp_path) == sorted(f"commute {run_id}" for run_id in run_ids)


def test_new_runs_are_analysed_others_come_from_cache(
    tmp_path: Path, spy: None
) -> None:
    meta = _project(tmp_path, seeds=[41, 42], threshold=0.5)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 2
    # Hide one completion marker: that run is not complete yet.
    late, early = _run_ids(tmp_path)
    marker = tmp_path / "campaign" / "runs" / f"{late}.parquet"
    hidden = tmp_path / "hidden.parquet"
    os.replace(marker, hidden)
    first = analyze_from_meta_config(meta)
    assert first["completed"] == 1
    assert _calls(tmp_path) == [f"commute {early}"]
    os.replace(hidden, marker)
    (tmp_path / "calls.txt").unlink()

    payload = analyze_from_meta_config(meta)

    assert payload["completed"] == 2
    assert _calls(tmp_path) == [f"commute {late}"]


def test_failing_script_fails_analyze_with_message(tmp_path: Path, spy: None) -> None:
    meta = _project(tmp_path)
    plan_from_meta_config(meta)
    run_from_meta_config(meta)
    _write_script(tmp_path, "commute", extra='raise ValueError("no commute here")')
    [run_id] = _run_ids(tmp_path)

    with pytest.raises(Exception) as error:
        analyze_from_meta_config(meta)

    assert str(error.value) == (
        f"analysis 'commute' failed for run {run_id}: ValueError: no commute here"
    )
    log = tmp_path / "campaign" / "analyses-cache" / "logs" / f"commute-{run_id}.log"
    assert "Traceback" in log.read_text()
    assert not (tmp_path / "campaign" / "analysis-generation.json").exists()


def test_script_error_fails_analyze_even_with_threshold_below_one(
    tmp_path: Path, spy: None
) -> None:
    meta = _project(tmp_path, seeds=[41, 42], threshold=0.5)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 2
    _write_script(
        tmp_path,
        "commute",
        extra='if parameters["seed"] == 42:\n        raise RuntimeError("seed 42")',
    )

    with pytest.raises(Exception, match=r"analysis 'commute' failed for run .*: RuntimeError: seed 42"):
        analyze_from_meta_config(meta)
    assert not (tmp_path / "campaign" / "analysis-generation.json").exists()


def test_publication_records_analysis_provenance(tmp_path: Path, spy: None) -> None:
    meta = _project(tmp_path)
    plan_from_meta_config(meta)
    run_from_meta_config(meta)

    analyze_from_meta_config(meta)

    [run_id] = _run_ids(tmp_path)
    manifest = _manifest(tmp_path)
    record = manifest["provenance"]["analyses"]["commute"][run_id]
    archive = tmp_path / "campaign" / "archives" / run_id
    [trips] = [entry for entry in archive.iterdir() if "trips" in entry.name]
    assert record["inputsSha256"] == {
        "trips": hashlib.sha256(trips.read_bytes()).hexdigest()
    }
    assert record["scriptSha256"] == lf_sha256(tmp_path / "analyses" / "commute.py")
    cached = tmp_path / "campaign" / "analyses-cache" / "commute" / (
        record["resultFingerprint"] + ".csv"
    )
    assert record["sha256"] == hashlib.sha256(cached.read_bytes()).hexdigest()
    assert record["size"] == cached.stat().st_size
    assert len(record["environmentFingerprint"]) == 64
    run_set_path = tmp_path / "campaign" / manifest["outputs"]["capturedRunSet"]["path"]
    run_set = json.loads(run_set_path.read_text())
    assert run_set["schemaVersion"] == 2
    [described] = run_set["runs"]
    assert described["analyses"] == {"commute": record}
    assert [item["name"] for item in described["artifacts"]] == ["age", "trips"]


def test_analyze_without_analyses_is_unchanged(
    tmp_path: Path, spy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Declared but read by no indicator: never computed, never probed.
    meta = _project(tmp_path, used=())

    def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("analysis machinery called")

    monkeypatch.setattr(analysis_execution, "environment_fingerprint", forbidden)
    monkeypatch.setattr(analysis_execution, "run_analysis", forbidden)
    plan_from_meta_config(meta)
    run_from_meta_config(meta)

    payload = analyze_from_meta_config(meta)

    assert payload["status"] == "completed"
    assert payload["warnings"] == []
    assert payload["indicators"] == 1
    assert not (tmp_path / "campaign" / "analyses-cache").exists()
    manifest = _manifest(tmp_path)
    assert manifest["provenance"]["analyses"] == {}
    run_set = json.loads(
        (tmp_path / "campaign" / manifest["outputs"]["capturedRunSet"]["path"]).read_text()
    )
    assert [run["analyses"] for run in run_set["runs"]] == [{}]


BARRIER = """\
import os
import time

import pandas

BARRIER = {barrier!r}


def compute(files, parameters):
    os.makedirs(BARRIER, exist_ok=True)
    marker = os.path.join(BARRIER, str(os.getpid()))
    open(marker, "w").close()
    seen = 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        seen = max(seen, len(os.listdir(BARRIER)))
        if seen >= 2:
            break
        time.sleep(0.02)
    end = time.monotonic() + 0.5
    while time.monotonic() < end:
        seen = max(seen, len(os.listdir(BARRIER)))
        time.sleep(0.02)
    os.remove(marker)
    with open(BARRIER + "-" + parameters["runId"], "w") as stream:
        stream.write(str(seen))
    return pandas.DataFrame({{"rows": [1.0]}})
"""


def test_analyses_run_in_parallel_up_to_workers(tmp_path: Path, spy: None) -> None:
    meta = _project(tmp_path, seeds=[41, 42, 43, 44], workers=2)
    barrier = tmp_path / "barrier"
    (tmp_path / "analyses" / "commute.py").write_text(
        BARRIER.format(barrier=str(barrier))
    )
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 4

    analyze_from_meta_config(meta)

    seen = [
        int(path.read_text())
        for path in tmp_path.iterdir()
        if path.name.startswith("barrier-")
    ]
    assert len(seen) == 4
    assert max(seen) == 2


def test_altered_cache_entry_is_recomputed_and_reported(
    tmp_path: Path, spy: None
) -> None:
    meta = _project(tmp_path)
    plan_from_meta_config(meta)
    run_from_meta_config(meta)
    analyze_from_meta_config(meta)
    [entry] = (tmp_path / "campaign" / "analyses-cache" / "commute").glob("*.csv")
    entry.write_text("rows\n99\n")

    payload = analyze_from_meta_config(meta)

    assert payload["warnings"] == [
        f"analysis cache entry commute/{entry.name} was altered; "
        "it was removed and recomputed"
    ]
    [run_id] = _run_ids(tmp_path)
    assert _values(payload)[run_id, "value(commute, rows)"] == 2.0
    assert len(_calls(tmp_path)) == 2


def test_init_rerun_accepts_analyses_cache(tmp_path: Path) -> None:
    _, root, model, data = initialize(tmp_path, "project")
    campaign_root = root / "campaigns" / "grid"
    campaign_root.mkdir()
    run_id = "a" * 32
    pq.write_table(pa.table({"runId": [run_id]}), campaign_root / "plan.parquet")
    result = tmp_path / "result.csv"
    result.write_text("rows\n2\n")
    AnalysisCache(campaign_root).put("commute", "f" * 64, result)
    analysis_execution._write_log(campaign_root, "commute", run_id, "Traceback\n")

    payload = init_from_inputs("project", root, model, data, None, invocation())

    assert payload["status"] == "unchanged"
    assert (campaign_root / "analyses-cache" / "commute" / ("f" * 64 + ".csv")).is_file()


def test_smoke_check_skips_indicators_on_analysis_outputs(
    tmp_path: Path, spy: None
) -> None:
    meta = _project(tmp_path, seeds=[41, 42])
    plan_from_meta_config(meta)
    loaded = load_meta_config(meta)
    specs = load_run_specs(
        loaded.campaign_root / "plan.parquet", _design(loaded), loaded.campaign
    )
    assert any(
        "analysis" in analysis_identity(indicator)["artifact"]
        for indicator in loaded.indicators
    )

    # Indicators on archived outputs are computed; analysis outputs come
    # from scripts that only analyze runs.
    checks._smoke(loaded, specs)

    assert _calls(tmp_path) == []
    assert not os.path.exists(tmp_path / "campaign" / "analyses-cache")


SELF_REWRITING = """\
import pandas

SOURCE = {source!r}


def compute(files, parameters):
    with open(SOURCE, "w", encoding="utf-8") as stream:
        stream.write(
            "import pandas\\n"
            "def compute(files, parameters):\\n"
            "    return pandas.DataFrame({{'rows': [5.0]}})\\n"
        )
    return pandas.DataFrame({{"rows": [2.0]}})
"""


def test_script_is_captured_once_per_analyze(tmp_path: Path, spy: None) -> None:
    meta = _project(tmp_path, seeds=[41, 42, 43])
    script = tmp_path / "analyses" / "commute.py"
    script.write_text(SELF_REWRITING.format(source=str(script)))
    original = lf_sha256(script)
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 3

    payload = analyze_from_meta_config(meta)

    assert lf_sha256(script) != original
    records = _manifest(tmp_path)["provenance"]["analyses"]["commute"]
    assert len(records) == 3
    assert {record["scriptSha256"] for record in records.values()} == {original}
    assert {
        value
        for (_, indicator), value in _values(payload).items()
        if indicator == "value(commute, rows)"
    } == {2.0}


STOPPING = """\
import os
import time

import pandas

PIDS = {pids!r}


def compute(files, parameters):
    os.makedirs(PIDS, exist_ok=True)
    pid = str(os.getpid())
    open(os.path.join(PIDS, pid), "w").close()
    if parameters["seed"] == 41:
        raise RuntimeError("stop")
    time.sleep(1.5)
    open(os.path.join(PIDS, "done-" + pid), "w").close()
    return pandas.DataFrame({{"rows": [1.0]}})
"""


def test_script_failure_leaves_no_script_running_and_frees_the_lease(
    tmp_path: Path, spy: None
) -> None:
    meta = _project(tmp_path, seeds=[41, 42], workers=2)
    pids = tmp_path / "pids"
    (tmp_path / "analyses" / "commute.py").write_text(STOPPING.format(pids=str(pids)))
    plan_from_meta_config(meta)
    assert run_from_meta_config(meta)["completed"] == 2

    with pytest.raises(Exception, match="RuntimeError: stop"):
        analyze_from_meta_config(meta)

    started = [path.name for path in pids.iterdir() if path.name.isdigit()]
    assert len(started) == 2
    # The other run's script was waited for, not abandoned.
    assert len([path for path in pids.iterdir() if path.name.startswith("done-")]) == 1
    for pid in started:
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)
    lock = tmp_path / "campaign" / ".eqasim-sa" / "locks" / "analysis.lock"
    with file_lease(lock, blocking=False):
        pass
