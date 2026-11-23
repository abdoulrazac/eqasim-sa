"""Meta-config entry point of ``eqasim-sa plot``."""

from pathlib import Path

from eqasim_sa.adapters.meta_config import load_meta_config
from eqasim_sa.core.figures.plot import plot_campaign


def plot_from_meta_config(path: Path, figures: tuple[str, ...] = ()) -> dict:
    """Draw the figures valid for the published analysis of a campaign."""

    meta = load_meta_config(path, verify_data=False, use_sealed_plan=True)
    return plot_campaign(meta.campaign_root, tuple(figures))
