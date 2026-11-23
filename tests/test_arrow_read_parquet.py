"""``read_parquet`` closes the file it opens and only that one."""

import pyarrow as pa
import pyarrow.parquet as pq

from eqasim_sa.utils import arrow


def _write(path):
    pq.write_table(pa.table({"a": [1, 2]}), path)


def test_read_parquet_closes_the_file_it_opens(tmp_path, monkeypatch):
    path = tmp_path / "t.parquet"
    _write(path)
    opened = []
    original = pq.ParquetFile

    def spy(*args, **kwargs):
        parquet = original(*args, **kwargs)
        opened.append(parquet)
        return parquet

    monkeypatch.setattr(arrow.pq, "ParquetFile", spy)

    assert arrow.read_parquet(path).num_rows == 2
    [parquet] = opened
    assert parquet.reader.closed


def test_read_parquet_leaves_a_caller_owned_file_open(tmp_path):
    path = tmp_path / "t.parquet"
    _write(path)
    with pq.ParquetFile(path) as parquet:
        assert arrow.read_parquet(parquet).num_rows == 2
        assert not parquet.reader.closed
