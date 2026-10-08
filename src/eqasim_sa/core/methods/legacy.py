"""Dispatch and compatibility exports for legacy scientific plans."""

import pyarrow as pa

from eqasim_sa.core.methods.legacy_canonical_morris import (
    LEGACY_CANONICAL_MORRIS_DOMAIN,
    _legacy_canonical_selection,
    _validate_legacy_canonical_morris,
)
from eqasim_sa.core.methods.legacy_common import _legacy_scientific_metadata
from eqasim_sa.core.methods.legacy_multiscramble_sobol import (
    LEGACY_MULTISCRAMBLE_SOBOL_DOMAIN,
    _validate_legacy_multiscramble_sobol,
)

def _validate_legacy_scientific_plan(
    plan: pa.Table,
    method: str,
    bootstrap_resamples: int | None,
    confidence_level: float | None,
    convergence_checkpoints: tuple[int, ...] | None,
    screening_top_k: int | None,
    structured: dict[str, dict[str, list[str]]],
    version: int,
) -> None:
    design, descriptor, context_hash = _legacy_scientific_metadata(
        plan, method, structured, version
    )
    if method == "canonical_morris":
        _validate_legacy_canonical_morris(
            plan,
            design,
            descriptor,
            context_hash,
            bootstrap_resamples,
            confidence_level,
            convergence_checkpoints,
            screening_top_k,
            version,
        )
        return
    _validate_legacy_multiscramble_sobol(
        plan,
        design,
        descriptor,
        context_hash,
        confidence_level,
        structured,
        version,
    )
