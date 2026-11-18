"""Translate a validated meta-configuration into a campaign design."""

from eqasim_sa.core.charter import serialize_scientific_contract
from eqasim_sa.core.plan import (
    CanonicalMorrisDesign,
    DiscreteOATDesign,
    GridDesign,
    MultiScrambleSobolDesign,
    SobolDesign,
)


def _validate_scientific_method(meta) -> None:  # type: ignore[no-untyped-def]
    status = getattr(getattr(meta, "charter", None), "status", "demonstration")
    if status == "scientific" and meta.method not in {"canonical_morris", "multiscramble_sobol"}:
        raise ValueError(
            "no current method satisfies the scientific contract; "
            f"{meta.method} is demonstration-only"
        )


def _design(meta):  # type: ignore[no-untyped-def]
    """Build the design whose base config carries the eqasim ``run:`` list as is."""

    _validate_scientific_method(meta)
    structured_factors = getattr(meta, "structured_factors", {})
    outputs = dict(meta.outputs)
    common = (
        meta.model_document,
        meta.factors,
        meta.seeds,
        {},
        meta.context_hash,
        meta.context_descriptor,
        meta.campaign_root,
    )
    if getattr(meta, "method", "grid") == "discrete_oat":
        return DiscreteOATDesign(
            *common,
            meta.trajectories,
            meta.design_seed,
            structured_factors=structured_factors,
            outputs=outputs,
        )
    if getattr(meta, "method", "grid") == "canonical_morris":
        distributions = {
            name: {
                "family": distribution.family,
                "transform": distribution.transform,
                "provenance": distribution.provenance,
            }
            for name, distribution in meta.distributions.items()
        }
        contract = serialize_scientific_contract(
            meta.charter,
            meta.factors,
            meta.distributions,
            meta.eqasim_sa_version,
            meta.eqasim_sa_sha256,
        )
        return CanonicalMorrisDesign(
            *common,
            meta.trajectories,
            meta.levels,
            meta.candidate_trajectories,
            meta.design_seed,
            distributions,
            contract,
            meta.bootstrap_resamples,
            meta.confidence_level,
            meta.convergence_checkpoints,
            meta.screening_top_k,
            structured_factors=structured_factors,
            outputs=outputs,
        )
    if getattr(meta, "method", "grid") == "sobol":
        return SobolDesign(
            *common,
            meta.base_sample_size,
            meta.design_seed,
            structured_factors=structured_factors,
            outputs=outputs,
        )
    if getattr(meta, "method", "grid") == "multiscramble_sobol":
        full_factors = {**meta.factors, "seed": list(meta.seeds)}
        distributions = {
            name: {
                "family": distribution.family,
                "transform": distribution.transform,
                "provenance": distribution.provenance,
            }
            for name, distribution in meta.distributions.items()
        }
        contract = serialize_scientific_contract(
            meta.charter,
            full_factors,
            meta.distributions,
            meta.eqasim_sa_version,
            meta.eqasim_sa_sha256,
        )
        return MultiScrambleSobolDesign(
            *common,
            meta.base_sample_sizes,
            meta.scramble_seeds,
            distributions,
            contract,
            meta.confidence_level,
            structured_factors=structured_factors,
            outputs=outputs,
        )
    return GridDesign(
        *common, structured_factors=structured_factors, outputs=outputs
    )
