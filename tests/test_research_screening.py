from __future__ import annotations

from tradehub_research.acceptance.runner import PACK_REGISTRY
from tradehub_research.funnel import FunnelConfig
from tradehub_research.screening import ScreeningConfig


def test_screening_config_defaults() -> None:
    config = ScreeningConfig.from_dict({})
    assert config.funnel == FunnelConfig()
    assert config.universe_coverage == ("SUPPORTED",)
    assert config.holdings == frozenset()


def test_funnel_config_hash_is_canonical_and_sensitive() -> None:
    assert FunnelConfig().config_hash == FunnelConfig().config_hash
    assert FunnelConfig().config_hash != FunnelConfig(budget=49).config_hash


def test_ra01_is_explicitly_registered_with_fifteen_assertions() -> None:
    assert len(PACK_REGISTRY["RA-01"]) == 15
