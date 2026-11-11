"""Provider factory: choose the real adapter or the stand-in from config.

The orchestrator only sees the ``ModelProvider`` interface. This function is the
single place where the environment decides which implementation to construct.
"""

from __future__ import annotations

from typing import Any

from the_relay.config import Settings
from the_relay.providers.base import ModelProvider
from the_relay.providers.groq import GroqProvider
from the_relay.providers.standin import StandInConfig, StandInProvider


def build_provider(
    settings: Settings,
    *,
    standin_overrides: dict[str, Any] | None = None,
) -> ModelProvider:
    """Construct the configured backend.

    ``standin_overrides`` lets an experiment change simulated limits without
    editing the JSON file, so a whole scenario is reproducible from one command.
    """
    if settings.provider == "groq":
        return GroqProvider(settings)
    path = settings.resolved(settings.config_dir) / "standin_calibration.json"
    config = StandInConfig.from_file(path, **(standin_overrides or {}))
    return StandInProvider(config)
