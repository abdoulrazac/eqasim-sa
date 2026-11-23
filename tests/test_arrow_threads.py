"""Static guard: pyarrow reads must never use arrow's worker threads.

A threaded read can leave a worker thread parked on the GIL while Python
shuts down, and the process then hangs forever at exit.  Parquet reads go
through ``eqasim_sa.utils.arrow.read_parquet``; CSV reads pass a
``ReadOptions(use_threads=False)``.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src" / "eqasim_sa" / "utils" / "arrow.py"


def _sources():
    for base in ("src", "protocols"):
        yield from sorted((ROOT / base).rglob("*.py"))


def _name(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _no_threads(call):
    return any(
        keyword.arg == "use_threads"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _violations(path, tree):
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func)
        where = f"{path.relative_to(ROOT)}:{node.lineno}"
        if name == "read_table" or (
            name == "read"
            and isinstance(node.func, ast.Attribute)
            and (
                (
                    isinstance(node.func.value, ast.Call)
                    and _name(node.func.value.func) == "ParquetFile"
                )
                or _name(node.func.value) in {"parquet", "parquet_file"}
            )
        ):
            if path != HELPER or not _no_threads(node):
                found.append(f"{where}: use eqasim_sa.utils.arrow.read_parquet")
        elif name == "ReadOptions" and not _no_threads(node):
            found.append(f"{where}: ReadOptions needs use_threads=False")
        elif name in {"read_csv", "open_csv"} and not any(
            keyword.arg == "read_options" for keyword in node.keywords
        ):
            found.append(f"{where}: {name} needs read_options with use_threads=False")
    return found


def test_pyarrow_reads_never_use_threads():
    found = []
    for path in _sources():
        found += _violations(path, ast.parse(path.read_text(encoding="utf-8")))
    assert not found, "\n".join(found)


def test_guard_detects_threaded_reads():
    tree = ast.parse(
        "pq.ParquetFile(p).read()\npq.read_table(p)\n"
        "csv.read_csv(s)\ncsv.ReadOptions(block_size=1)\n"
        "parquet.read()\ncsv.ReadOptions(use_threads=False)\n"
    )
    assert len(_violations(ROOT / "src" / "x.py", tree)) == 5
