"""Exclusive, disposable worker workspace for the eqasim adapter."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from eqasim_sa.utils.filesystem import fsync_directory
from eqasim_sa.utils.names import validate_portable_name


_CAMPAIGN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SLOT = re.compile(r"^(0|[1-9][0-9]*)$")
_ATTEMPT = re.compile(r"^attempt-[0-9a-f]{32}$")
_STATE_TEMP = re.compile(r"^\.state-[0-9a-f]{32}\.tmp$")
_CONTEXT_STATE = re.compile(r"^state-[0-9a-f]{64}\.json$")


class WorkerWorkspace:
    """Own a worker slot's topology, lease, cache state, and measurements."""

    def __init__(
        self,
        scratch: Path,
        campaign: str,
        slot: int,
        context_hash: str,
        interrupted: Callable[[], bool],
    ) -> None:
        if type(slot) is not int or slot < 0:
            raise ValueError("worker slot must be a non-negative integer")
        if type(campaign) is not str or _CAMPAIGN.fullmatch(campaign) is None:
            raise ValueError("campaign must be one safe path segment")
        validate_portable_name(campaign, "campaign")
        if (
            type(context_hash) is not str
            or _CONTEXT.fullmatch(context_hash) is None
        ):
            raise ValueError("context_hash must be one safe non-empty path segment")
        validate_portable_name(context_hash, "context_hash")
        self.scratch = scratch.resolve(strict=True)
        self.campaign = campaign
        self.slot = slot
        self.context_hash = context_hash
        self.root = self.scratch / "eqasim-sa" / campaign / "workers" / str(slot)
        self.cache = self.root / "cache" / context_hash
        self._state = self.root / f"state-{context_hash}.json"
        self._lease = self.root / "lease.lock"
        self._interrupted = interrupted
        self._descriptor: int | None = None
        self._root_identity: tuple[int, int] | None = None
        self._session_lock = RLock()

    @contextmanager
    def session(self) -> Iterator["WorkerWorkspace"]:
        with self._session_lock:
            if self._descriptor is not None:
                yield self
                return
            self._prepare_root()
            descriptor = os.open(
                self._lease, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                ):
                    raise ValueError("worker lease must be a private owned regular file")
                os.fchmod(descriptor, 0o600)
                while True:
                    if self._interrupted():
                        raise KeyboardInterrupt
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
                self._descriptor = descriptor
                root_info = self.root.stat(follow_symlinks=False)
                self._root_identity = (root_info.st_dev, root_info.st_ino)
                self.validate_session()
                yield self
            finally:
                self._descriptor = None
                self._root_identity = None
                os.close(descriptor)

    def validate_session(self) -> None:
        if self._descriptor is None or self._root_identity is None:
            raise RuntimeError("worker cache lease is not held")
        root = self.root.stat(follow_symlinks=False)
        held = os.fstat(self._descriptor)
        visible = self._lease.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(root.st_mode)
            or (root.st_dev, root.st_ino) != self._root_identity
            or not stat.S_ISREG(visible.st_mode)
            or (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino)
        ):
            raise RuntimeError("worker slot or lease identity changed while held")

    def prepare_cache(self) -> Path:
        self.validate_session()
        if self.cache.parent != self.root:
            parent = self.cache.parent
            if parent.is_symlink() or parent.exists() and not parent.is_dir():
                raise ValueError("worker cache namespace must be a non-symlink directory")
            parent.mkdir(mode=0o700, exist_ok=True)
            info = parent.stat(follow_symlinks=False)
            if info.st_uid != os.getuid():
                raise ValueError("worker cache namespace must be owned by the current user")
            parent.chmod(0o700)
        state = self._read_state()
        if self.cache.exists() or self.cache.is_symlink():
            try:
                self._validate_cache()
            except ValueError:
                self._purge_owned_cache()
            else:
                if state != "clean":
                    self._purge_owned_cache()
        self.cache.mkdir(mode=0o700, exist_ok=True)
        self._validate_cache()
        return self.cache

    def mark_active(self) -> None:
        self._write_state("active")

    def mark_clean(self) -> None:
        self.validate_session()
        self._validate_cache()
        self._write_state("clean")

    def status(self) -> str | None:
        return self._read_state()

    def create_attempt(self) -> Path:
        self.validate_session()
        attempt = self.root / f"attempt-{uuid.uuid4().hex}"
        attempt.mkdir(mode=0o700)
        return attempt

    def cache_bytes(self) -> int:
        self.validate_session()
        self._validate_cache()
        measured = sum(
            path.stat(follow_symlinks=False).st_size
            for path in self.cache.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        self._validate_cache()
        return measured

    def _validate_cache(self) -> None:
        try:
            info = self.cache.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError("worker cache must be an owned non-symlink directory") from error
        if (
            self.cache.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise ValueError("worker cache must be an owned non-symlink directory")
        self._validate_tree(self.cache, "worker cache")

    def _purge_owned_cache(self) -> None:
        info = self.cache.stat(follow_symlinks=False)
        if info.st_uid != os.getuid():
            raise ValueError("worker cache must be owned by the current user")
        if self.cache.is_symlink() or not stat.S_ISDIR(info.st_mode):
            self.cache.unlink()
            return
        self._validate_tree_ownership(self.cache, "worker cache")
        shutil.rmtree(self.cache)

    def _prepare_root(self) -> None:
        current = self.scratch
        for part in ("eqasim-sa", self.campaign, "workers", str(self.slot)):
            current = current / part
            if current.is_symlink():
                raise ValueError("worker slot must not traverse a symlink")
            current.mkdir(mode=0o700, exist_ok=True)
            if not current.is_dir():
                raise ValueError("worker slot must be a directory")
            info = current.stat(follow_symlinks=False)
            if info.st_uid != os.getuid():
                raise ValueError("worker slot must be owned by the current user")
            current.chmod(0o700)
        if current.resolve(strict=True) != self.root.resolve(strict=True):
            raise ValueError("worker slot path mismatch")

    def _read_state(self) -> str | None:
        if not (self._state.exists() or self._state.is_symlink()):
            return None
        if self._state.is_symlink() or not self._state.is_file():
            return "invalid"
        try:
            info = self._state.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
            ):
                return "invalid"
            document = json.loads(self._state.read_text(encoding="utf-8"))
            if (
                isinstance(document, dict)
                and set(document) == {"status"}
                and document.get("status") in {"active", "clean"}
            ):
                return str(document["status"])
        except (OSError, UnicodeError, ValueError):
            pass
        return "invalid"

    def _write_state(self, status: str) -> None:
        self.validate_session()
        temporary = self.root / f".state-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                temporary.chmod(0o600)
                json.dump({"status": status}, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state)
            fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_tree(root: Path, description: str) -> None:
        for path in root.rglob("*"):
            info = path.stat(follow_symlinks=False)
            if path.is_symlink() or info.st_uid != os.getuid():
                raise ValueError(f"{description} entries must be owned non-symlinks")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ValueError(f"{description} files must not be hard links")
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{description} entries must be regular files or directories")

    @staticmethod
    def _validate_tree_ownership(root: Path, description: str) -> None:
        for path in root.rglob("*"):
            if path.stat(follow_symlinks=False).st_uid != os.getuid():
                raise ValueError(f"{description} entries must be owned by the current user")

    @staticmethod
    def _validate_owned_directory(path: Path, description: str) -> None:
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as error:
            raise FileExistsError(f"{description} must be an owned directory") from error
        if (
            path.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise FileExistsError(f"{description} must be an owned directory")

    @classmethod
    def validate_scratch(cls, root: Path, campaigns: set[str]) -> None:
        """Validate all disposable eqasim workspaces accepted by project init."""
        cls._validate_owned_directory(root, "scratch")
        entries = tuple(root.iterdir())
        if not entries:
            return
        if (
            len(entries) != 1
            or entries[0].name != "eqasim-sa"
            or entries[0].is_symlink()
            or not entries[0].is_dir()
        ):
            raise FileExistsError("foreign content in init scratch")
        cls._validate_owned_directory(entries[0], "scratch/eqasim-sa")
        for campaign in entries[0].iterdir():
            if (
                campaign.name not in campaigns
                or campaign.is_symlink()
                or not campaign.is_dir()
            ):
                raise FileExistsError("foreign content in init scratch")
            cls._validate_owned_directory(campaign, "scratch campaign")
            campaign_entries = {item.name for item in campaign.iterdir()}
            if campaign_entries - {"workers"}:
                raise FileExistsError("foreign content in init scratch")
            if not campaign_entries:
                continue
            workers = campaign / "workers"
            if workers.is_symlink() or not workers.is_dir():
                raise FileExistsError("foreign content in init scratch")
            cls._validate_owned_directory(workers, "scratch workers")
            for slot in workers.iterdir():
                if (
                    slot.is_symlink()
                    or not slot.is_dir()
                    or _SLOT.fullmatch(slot.name) is None
                ):
                    raise FileExistsError("foreign content in init scratch")
                cls._validate_owned_directory(slot, "worker slot")
                cls._validate_slot_inventory(slot)

    @classmethod
    def _validate_slot_inventory(cls, slot: Path) -> None:
        for item in slot.iterdir():
            attempt = _ATTEMPT.fullmatch(item.name) is not None
            state_temp = _STATE_TEMP.fullmatch(item.name) is not None
            context_state = _CONTEXT_STATE.fullmatch(item.name) is not None
            if item.is_symlink() or (
                item.name not in {"lease.lock", "state.json", "cache"}
                and not context_state
                and not attempt
                and not state_temp
            ):
                raise FileExistsError("foreign content in init scratch")
            if item.name in {"lease.lock", "state.json"} or context_state or state_temp:
                info = item.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                ):
                    raise FileExistsError("foreign content in init scratch")
            elif item.name == "cache":
                if not item.is_dir():
                    raise FileExistsError("foreign content in init scratch")
                try:
                    cls._validate_owned_directory(item, "worker cache")
                    cls._validate_tree(item, "worker cache")
                except ValueError as error:
                    raise FileExistsError("foreign content in init scratch") from error
            elif attempt:
                if not item.is_dir() or {path.name for path in item.iterdir()} - {
                    "config.yml", "run.log", "output", "normalized"
                }:
                    raise FileExistsError("foreign content in init scratch")
                try:
                    cls._validate_owned_directory(item, "worker attempt")
                    cls._validate_tree(item, "worker attempt")
                except ValueError as error:
                    raise FileExistsError("foreign content in init scratch") from error
