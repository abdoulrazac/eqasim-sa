import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from eqasim_sa.ports.simulator import ResolvedConfig

CAMPAIGN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STAGES = ["synthesis.output", "analysis.synthesis.population"]


@dataclass(frozen=True)
class EqasimSettings:
    config_path: Path
    eqasim_root: Path
    invocation: tuple[str, ...]
    campaign: str
    scratch: Path

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
        object.__setattr__(self, "config_path", config)
        object.__setattr__(self, "eqasim_root", root)
        object.__setattr__(self, "scratch", scratch)

    @property
    def workdir(self) -> Path:
        return self.scratch / "eqasim-sa" / self.campaign / "workdir"


def load_config(path: Path, seed: int) -> ResolvedConfig:
    document = load_yaml_document(path)
    validate_eqasim_document(document)
    resolved = copy.deepcopy(document)
    resolved["run"] = list(STAGES)
    resolved["config"]["random_seed"] = seed
    return resolved


def validate_eqasim_document(document: object) -> None:
    if not isinstance(document, dict):
        raise TypeError("eqasim YAML root must be a mapping")
    if not isinstance(document.get("working_directory"), str):
        raise TypeError("working_directory must be a string")
    if not isinstance(document.get("run"), list):
        raise TypeError("run must be a list")
    if not isinstance(document.get("config"), dict):
        raise TypeError("config must be a mapping")


def load_yaml_document(path: Path) -> object:
    return load_yaml_source(path.read_text(encoding="utf-8"))


def load_yaml_source(source: str) -> object:
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
