"""Pure validity catalogue: which figures make sense for a method and published tables."""

from dataclasses import dataclass

METHODS = ("grid", "discrete_oat", "sobol", "canonical_morris", "multiscramble_sobol")


@dataclass(frozen=True)
class FigureSpec:
    name: str
    method: str | None  # None = every method
    required_roles: tuple[str, ...]


FIGURES: tuple[FigureSpec, ...] = (
    FigureSpec("output-summary", None, ("outputSummary",)),
    FigureSpec("grid-response", "grid", ("dispersion",)),
    FigureSpec("oat-effects", "discrete_oat", ("sensitivity",)),
    FigureSpec("sobol-indices", "sobol", ("sensitivity",)),
    FigureSpec("morris-plane", "canonical_morris", ("sensitivity",)),
    FigureSpec("morris-convergence", "canonical_morris", ("convergence",)),
    FigureSpec("morris-rank-reversals", "canonical_morris", ("rankReversals",)),
    FigureSpec("morris-noise-floor", "canonical_morris", ("noiseFloor",)),
    FigureSpec("multiscramble-indices", "multiscramble_sobol", ("sensitivity",)),
    FigureSpec("multiscramble-convergence", "multiscramble_sobol", ("convergence", "governedReport")),
    FigureSpec("multiscramble-by-scramble", "multiscramble_sobol", ("byScramble",)),
)


def applicable_figures(method: str, roles: frozenset[str]) -> tuple[FigureSpec, ...]:
    if method not in METHODS:
        raise ValueError(f"unsupported method {method!r}; supported: {', '.join(METHODS)}")
    return tuple(
        spec
        for spec in FIGURES
        if spec.method in (None, method) and all(role in roles for role in spec.required_roles)
    )


def select_figures(
    method: str, roles: frozenset[str], requested: tuple[str, ...]
) -> tuple[FigureSpec, ...]:
    applicable = applicable_figures(method, roles)
    if not requested:
        return applicable
    by_name = {spec.name: spec for spec in applicable}
    for name in requested:
        if name not in by_name:
            valid = ", ".join(by_name)
            raise ValueError(f"figure {name!r} is not valid for method {method!r}; valid: {valid}")
    return tuple(by_name[name] for name in dict.fromkeys(requested))
