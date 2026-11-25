"""Windows updates a file's change time when it is opened or read.

On Windows ``st_ctime_ns`` is the NTFS change time, which the system updates
when a file is merely opened or read, so a path ``lstat`` and the ``fstat`` of
the opened handle legitimately differ there. Stability checks must not compare
it on Windows; they keep comparing it on POSIX, where it is a metadata change.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from eqasim_sa.adapters import data_manifest
from eqasim_sa.adapters.data_manifest import generate_data_manifest
from eqasim_sa.utils import filesystem, platform


class _Shifted:
    """A stat result whose change time moved, as an open does on Windows."""

    def __init__(self, info: os.stat_result) -> None:
        self._info = info

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        value = getattr(self._info, name)
        if name == "st_ctime_ns":
            return value + 2_990_400
        return value


def _shift_fstat(monkeypatch: pytest.MonkeyPatch, module) -> None:  # type: ignore[no-untyped-def]
    real = os.fstat
    monkeypatch.setattr(module.os, "fstat", lambda descriptor: _Shifted(real(descriptor)))


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "eqasim-data"
    (data / "ban_lyon").mkdir(parents=True)
    (data / "ban_lyon" / "adresses-01.csv.gz").write_bytes(b"addresses")
    return data


def test_change_time_is_compared_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    info = os.stat(__file__)

    assert platform.change_time_ns(info) == info.st_ctime_ns


def test_change_time_is_ignored_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    info = os.stat(__file__)

    assert platform.change_time_ns(info) == 0
    assert filesystem.file_state(info) == filesystem.file_state(_Shifted(info))


def test_file_state_keeps_change_time_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    info = os.stat(__file__)

    assert filesystem.file_state(info) != filesystem.file_state(_Shifted(info))


def test_data_manifest_accepts_a_change_time_moved_by_opening_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    _shift_fstat(monkeypatch, data_manifest)
    data = _data(tmp_path)
    destination = tmp_path / "manifest.json"

    generate_data_manifest(data, destination)

    files = json.loads(destination.read_bytes())["files"]
    assert files == [
        {
            "path": "ban_lyon/adresses-01.csv.gz",
            "size": 9,
            "sha256": hashlib.sha256(b"addresses").hexdigest(),
        }
    ]


def test_data_manifest_still_refuses_a_moved_change_time_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    _shift_fstat(monkeypatch, data_manifest)
    data = _data(tmp_path)

    with pytest.raises(
        RuntimeError, match="data entry changed while opening: ban_lyon/adresses-01.csv.gz"
    ):
        generate_data_manifest(data, tmp_path / "manifest.json")
