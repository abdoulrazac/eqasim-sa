"""Line-ending-independent digests of text inputs that travel through Git."""

import hashlib
import json
from pathlib import Path, PurePosixPath


TEXT_SUFFIXES = frozenset(
    {".py", ".pyi", ".txt", ".md", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini"}
)


def lf_bytes(content: bytes) -> bytes:
    """Normalize CRLF line endings to LF without touching other bytes."""

    return content.replace(b"\r\n", b"\n")


def lf_sha256(path: Path) -> str:
    """Hash a file's content after CRLF to LF normalization."""

    return hashlib.sha256(lf_bytes(Path(path).read_bytes())).hexdigest()


def is_text_name(relative_path: str) -> bool:
    """True for files whose identity must ignore line endings.

    Known text suffixes, plus suffixless metadata files (RECORD, METADATA,
    WHEEL, ...) inside a ``*.dist-info`` directory. Everything else is binary.
    """

    path = PurePosixPath(relative_path)
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    return not path.suffix and any(
        part.endswith(".dist-info") for part in path.parts[:-1]
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for name, value in pairs:
        if name in document:
            raise ValueError(f"duplicate JSON key: {name}")
        document[name] = value
    return document


def canonical_json_sha256(payload: bytes) -> str:
    """Hash the deterministic re-serialization of a JSON document."""

    try:
        document = json.loads(payload, object_pairs_hook=_unique_object)
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("JSON document cannot be canonicalized") from error
    return hashlib.sha256(canonical).hexdigest()
