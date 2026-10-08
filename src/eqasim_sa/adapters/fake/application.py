"""Application entry point backed by the deterministic fake simulator."""

from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.cli import dispatch


def main() -> int:
    """Run the CLI with deterministic fake simulator and local backend adapters."""

    return dispatch(FakeSimulator(), LocalBackend())
