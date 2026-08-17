from eqasim_sa.adapters.backends.local import LocalBackend
from eqasim_sa.adapters.fake.simulator import FakeSimulator
from eqasim_sa.cli import dispatch


def main() -> int:
    return dispatch(FakeSimulator(), LocalBackend())
