"""Exclusive, disposable worker workspace for the eqasim adapter."""

from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from eqasim_sa.utils import platform
from eqasim_sa.utils.filesystem import (
    owned_by_current_user,
    stable_file_bytes,
    write_bytes_atomic,
)
from eqasim_sa.utils.names import validate_portable_name


CAMPAIGN_MAX = 64
SLOT_MAX = 4
_PREFIX_HEX = 12
# Longest relative path eqasim-sa adds below the scratch directory:
# /esa/<campaign>/w<slot>/a<12 hex> (the cache directory c<12 hex> has the same
# length and eqasim runs inside the attempt directory).
WORKSPACE_PREFIX_BUDGET = (
    len("/esa//w/a") + CAMPAIGN_MAX + SLOT_MAX + _PREFIX_HEX
)


def workspace_prefix_length(campaign: str) -> int:
    """Longest relative path eqasim-sa adds below scratch for ``campaign``."""
    return len("/esa//w/a") + len(campaign) + SLOT_MAX + _PREFIX_HEX

_CAMPAIGN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CONTEXT = re.compile(r"^[0-9a-f]{64}$")
_SLOT = re.compile(r"^(0|[1-9][0-9]{0,3})$")
_SLOT_DIRECTORY = re.compile(r"^w(0|[1-9][0-9]{0,3})$")
_ATTEMPT = re.compile(r"^a[0-9a-f]{12}$")
_CACHE = re.compile(r"^c[0-9a-f]{12}$")
_IDENTITY = re.compile(r"^c[0-9a-f]{12}\.json$")
_STATE = re.compile(r"^s[0-9a-f]{12}\.json$")
_DURABLE_TEMP = re.compile(r"^\.[tb][0-9a-f]{8}$")


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
        if type(slot) is not int or slot < 0 or len(str(slot)) > SLOT_MAX:
            raise ValueError(
                f"worker slot must be a non-negative integer of at most {SLOT_MAX} digits"
            )
        if type(campaign) is not str or _CAMPAIGN.fullmatch(campaign) is None:
            raise ValueError("campaign must be one safe path segment")
        if len(campaign) > CAMPAIGN_MAX:
            raise ValueError(f"campaign must have at most {CAMPAIGN_MAX} characters")
        validate_portable_name(campaign, "campaign")
        if type(context_hash) is not str or _CONTEXT.fullmatch(context_hash) is None:
            raise ValueError("context_hash must be 64 lowercase hexadecimal characters")
        self.scratch = scratch.resolve(strict=True)
        self.campaign = campaign
        self.slot = slot
        self.context_hash = context_hash
        prefix = context_hash[:_PREFIX_HEX]
        self.root = self.scratch / "esa" / campaign / f"w{slot}"
        self.cache = self.root / f"c{prefix}"
        self._identity = self.root / f"c{prefix}.json"
        self._state = self.root / f"s{prefix}.json"
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
            if platform.is_link(self._lease):
                raise ValueError("worker lease must be a private owned regular file")
            descriptor = os.open(
                self._lease,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            locked = False
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not owned_by_current_user(info)
                ):
                    raise ValueError("worker lease must be a private owned regular file")
                self._lease.chmod(0o600)
                while not locked:
                    if self._interrupted():
                        raise KeyboardInterrupt
                    locked = platform.lock_exclusive_nonblocking(descriptor)
                    if not locked:
                        time.sleep(0.05)
                self._descriptor = descriptor
                root_info = self.root.stat(follow_symlinks=False)
                self._root_identity = (root_info.st_dev, root_info.st_ino)
                self.validate_session()
                self._verify_identity()
                yield self
            finally:
                self._descriptor = None
                self._root_identity = None
                try:
                    if locked:
                        platform.unlock(descriptor)
                finally:
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
        self._verify_identity()
        state = self._read_state()
        if self.cache.exists() or platform.is_link(self.cache):
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
        attempt = self.root / f"a{uuid.uuid4().hex[:_PREFIX_HEX]}"
        attempt.mkdir(mode=0o700)
        return attempt

    def cache_bytes(self) -> int:
        self.validate_session()
        self._validate_cache()
        measured = sum(
            path.stat(follow_symlinks=False).st_size
            for path in self.cache.rglob("*")
            if path.is_file() and not platform.is_link(path)
        )
        self._validate_cache()
        return measured

    def _validate_cache(self) -> None:
        try:
            info = self.cache.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError("worker cache must be an owned non-symlink directory") from error
        if (
            platform.is_link(self.cache)
            or not stat.S_ISDIR(info.st_mode)
            or not owned_by_current_user(info)
        ):
            raise ValueError("worker cache must be an owned non-symlink directory")
        self._validate_tree(self.cache, "worker cache")

    def _purge_owned_cache(self) -> None:
        info = self.cache.stat(follow_symlinks=False)
        if not owned_by_current_user(info):
            raise ValueError("worker cache must be owned by the current user")
        if platform.is_link(self.cache) or not stat.S_ISDIR(info.st_mode):
            platform.remove_file(self.cache)
            return
        self._validate_tree_ownership(self.cache, "worker cache")
        platform.remove_tree(self.cache)

    def _prepare_root(self) -> None:
        current = self.scratch
        for part in ("esa", self.campaign, f"w{self.slot}"):
            current = current / part
            if platform.is_link(current):
                raise ValueError("worker slot must not traverse a symlink")
            current.mkdir(mode=0o700, exist_ok=True)
            if not current.is_dir():
                raise ValueError("worker slot must be a directory")
            info = current.stat(follow_symlinks=False)
            if not owned_by_current_user(info):
                raise ValueError("worker slot must be owned by the current user")
            current.chmod(0o700)
        if current.resolve(strict=True) != self.root.resolve(strict=True):
            raise ValueError("worker slot path mismatch")

    def _verify_identity(self) -> None:
        """Create or verify the file holding the full campaign and context hash."""
        expected = {
            "schemaVersion": 1,
            "campaign": self.campaign,
            "contextHash": self.context_hash,
        }
        if not (self._identity.exists() or platform.is_link(self._identity)):
            write_bytes_atomic(
                (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                ),
                self._identity,
            )
            return
        try:
            info = self._identity.stat(follow_symlinks=False)
            if (
                platform.is_link(self._identity)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not owned_by_current_user(info)
            ):
                raise ValueError("invalid")
            content, _ = stable_file_bytes(self._identity)
            document = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError("worker identity file is invalid") from error
        if document == expected:
            return
        if (
            isinstance(document, dict)
            and document.get("campaign") == self.campaign
            and isinstance(document.get("contextHash"), str)
            and document["contextHash"] != self.context_hash
            and document["contextHash"][:_PREFIX_HEX] == self.context_hash[:_PREFIX_HEX]
        ):
            raise ValueError(
                "context hash prefix collision: worker slot already holds a "
                "different context sharing the same 12-character prefix"
            )
        raise ValueError("worker identity file does not match this campaign and context")

    def _read_state(self) -> str | None:
        if not (self._state.exists() or platform.is_link(self._state)):
            return None
        if platform.is_link(self._state) or not self._state.is_file():
            return "invalid"
        try:
            info = self._state.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not owned_by_current_user(info)
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
        write_bytes_atomic(
            (json.dumps({"status": status}, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
            self._state,
        )

    @staticmethod
    def _validate_tree(root: Path, description: str) -> None:
        for path in root.rglob("*"):
            info = path.stat(follow_symlinks=False)
            if platform.is_link(path) or not owned_by_current_user(info):
                raise ValueError(f"{description} entries must be owned non-symlinks")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ValueError(f"{description} files must not be hard links")
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{description} entries must be regular files or directories")

    @staticmethod
    def _validate_tree_ownership(root: Path, description: str) -> None:
        for path in root.rglob("*"):
            if not owned_by_current_user(path.stat(follow_symlinks=False)):
                raise ValueError(f"{description} entries must be owned by the current user")

    @staticmethod
    def _validate_owned_directory(path: Path, description: str) -> None:
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as error:
            raise FileExistsError(f"{description} must be an owned directory") from error
        if (
            platform.is_link(path)
            or not stat.S_ISDIR(info.st_mode)
            or not owned_by_current_user(info)
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
            or entries[0].name != "esa"
            or platform.is_link(entries[0])
            or not entries[0].is_dir()
        ):
            raise FileExistsError("foreign content in init scratch")
        cls._validate_owned_directory(entries[0], "scratch/esa")
        for campaign in entries[0].iterdir():
            if (
                campaign.name not in campaigns
                or platform.is_link(campaign)
                or not campaign.is_dir()
            ):
                raise FileExistsError("foreign content in init scratch")
            cls._validate_owned_directory(campaign, "scratch campaign")
            for slot in campaign.iterdir():
                if (
                    platform.is_link(slot)
                    or not slot.is_dir()
                    or _SLOT_DIRECTORY.fullmatch(slot.name) is None
                ):
                    raise FileExistsError("foreign content in init scratch")
                cls._validate_owned_directory(slot, "worker slot")
                cls._validate_slot_inventory(slot)

    @classmethod
    def _validate_slot_inventory(cls, slot: Path) -> None:
        for item in slot.iterdir():
            name = item.name
            attempt = _ATTEMPT.fullmatch(name) is not None
            cache = _CACHE.fullmatch(name) is not None
            file_entry = (
                name == "lease.lock"
                or _IDENTITY.fullmatch(name) is not None
                or _STATE.fullmatch(name) is not None
                or _DURABLE_TEMP.fullmatch(name) is not None
            )
            if platform.is_link(item) or not (file_entry or cache or attempt):
                raise FileExistsError("foreign content in init scratch")
            if file_entry:
                info = item.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or not owned_by_current_user(info)
                    or info.st_nlink != 1
                ):
                    raise FileExistsError("foreign content in init scratch")
            elif cache:
                if not item.is_dir():
                    raise FileExistsError("foreign content in init scratch")
                try:
                    cls._validate_owned_directory(item, "worker cache")
                    cls._validate_tree(item, "worker cache")
                except ValueError as error:
                    raise FileExistsError("foreign content in init scratch") from error
            else:
                if not item.is_dir() or {path.name for path in item.iterdir()} - {
                    "config.yml", "run.log", "output", "normalized"
                }:
                    raise FileExistsError("foreign content in init scratch")
                try:
                    cls._validate_owned_directory(item, "worker attempt")
                    cls._validate_tree(item, "worker attempt")
                except ValueError as error:
                    raise FileExistsError("foreign content in init scratch") from error
