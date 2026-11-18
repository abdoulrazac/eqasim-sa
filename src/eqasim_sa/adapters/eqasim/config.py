"""Load, validate, and resolve eqasim YAML configuration documents."""

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from eqasim_sa.adapters.eqasim.workspace import WorkerWorkspace
from eqasim_sa.utils.names import validate_portable_name
from eqasim_sa.ports.simulator import ResolvedConfig

CAMPAIGN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class EqasimSettings:
    """Validated paths and invocation shared by one eqasim worker slot."""

    config_path: Path
    eqasim_root: Path
    invocation: tuple[str, ...]
    campaign: str
    scratch: Path
    context_hash: str

    def __post_init__(self) -> None:
        config = self.config_path.resolve(strict=True)
        root = self.eqasim_root.resolve(strict=True)
        scratch = self.scratch.resolve(strict=True)
        if not config.is_file():
            raise ValueError("config_path must be a regular file")
        if not root.is_dir() or not scratch.is_dir():
            raise ValueError("eqasim_root and scratch must be directories")
        if not self.invocation or not all(
            isinstance(item, str) and item and "\0" not in item
            for item in self.invocation
        ):
            raise ValueError("invocation must contain non-empty arguments")
        if not CAMPAIGN.fullmatch(self.campaign):
            raise ValueError("campaign must be one safe path segment")
        validate_portable_name(self.campaign, "campaign")
        if type(self.context_hash) is not str or not CAMPAIGN.fullmatch(
            self.context_hash
        ):
            raise ValueError("context_hash must be one safe non-empty path segment")
        object.__setattr__(self, "config_path", config)
        object.__setattr__(self, "eqasim_root", root)
        object.__setattr__(self, "scratch", scratch)

    def workspace(
        self,
        slot: int,
        interrupted: Callable[[], bool] = lambda: False,
    ) -> WorkerWorkspace:
        return WorkerWorkspace(
            self.scratch,
            self.campaign,
            slot,
            self.context_hash,
            interrupted,
        )

    @property
    def workdir(self) -> Path:
        return self.workspace(0).root


def load_config(path: Path, seed: int) -> ResolvedConfig:
    """Resolve a model YAML for execution and inject the requested run seed.

    The ``run:`` list is kept as written: synpp reads it from the config.
    """

    document = load_yaml_document(path)
    validate_eqasim_document(document)
    resolved = copy.deepcopy(document)
    resolved["config"]["random_seed"] = seed
    return resolved


def validate_eqasim_document(document: object) -> None:
    """Validate the minimal eqasim mapping required by the execution adapter."""

    if not isinstance(document, dict):
        raise TypeError("eqasim YAML root must be a mapping")
    if not isinstance(document.get("working_directory"), str):
        raise TypeError("working_directory must be a string")
    run_stages(document.get("run"))
    if not isinstance(document.get("config"), dict):
        raise TypeError("config must be a mapping")


def run_stages(run: object) -> tuple[str, ...]:
    """Return the stage names of an eqasim ``run:`` list.

    Each item is a stage name or a one-entry ``{stage: parameters}`` mapping.
    """

    if not isinstance(run, list) or not run:
        raise TypeError("run must be a non-empty list of stages")
    stages = []
    for item in run:
        if isinstance(item, dict) and len(item) == 1:
            item = next(iter(item))
        if not isinstance(item, str) or not item:
            raise TypeError(
                "run items must be stage names or one-entry {stage: parameters} mappings"
            )
        stages.append(item)
    return tuple(stages)


def load_yaml_document(path: Path) -> object:
    """Load a UTF-8 YAML file under the project's strict YAML rules."""

    return load_yaml_source(path.read_text(encoding="utf-8"))


def load_yaml_source(source: str) -> object:
    """Parse JSON-compatible YAML while rejecting aliases and duplicate keys."""

    node = yaml.compose(source, Loader=yaml.SafeLoader)
    if node is not None:
        _validate_yaml_nodes(node, set())
    document = yaml.safe_load(source)
    try:
        json.dumps(document, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise TypeError("eqasim YAML must be JSON-compatible") from error
    return document


def _validate_yaml_nodes(node: yaml.Node, seen: set[int]) -> None:
    identity = id(node)
    if identity in seen:
        raise ValueError("YAML aliases are not supported")
    seen.add(identity)
    if isinstance(node, yaml.MappingNode):
        keys: set[tuple[str, str]] = set()
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise TypeError("YAML mapping keys must be scalar")
            if key.tag != "tag:yaml.org,2002:str":
                raise TypeError("YAML mapping keys must resolve to strings")
            identity_key = (key.tag, key.value)
            if identity_key in keys:
                raise ValueError(f"duplicate YAML key: {key.value!r}")
            keys.add(identity_key)
            _validate_yaml_nodes(key, seen)
            _validate_yaml_nodes(value, seen)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            _validate_yaml_nodes(value, seen)
