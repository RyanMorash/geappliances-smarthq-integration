"""Pytest plugins for SmartHQ integration tests."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.loader import (
    DATA_CUSTOM_COMPONENTS,
    DATA_INTEGRATIONS,
    _resolve_integrations_from_root,
)
from homeassistant.setup import async_setup_component

import custom_components
from custom_components.smarthq.const import DOMAIN

# Import before the test `hass` fixture initializes so HA discovers this repo's
# custom integration (pytest's stub `custom_components` package otherwise wins).
import custom_components.smarthq.config_flow  # noqa: F401

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
async def load_smarthq_integration(hass: HomeAssistant) -> None:
    """Load application_credentials and register SmartHQ for config flow tests."""
    integration = _resolve_integrations_from_root(hass, custom_components, [DOMAIN])[
        DOMAIN
    ]
    hass.data[DATA_INTEGRATIONS][DOMAIN] = integration
    hass.data[DATA_CUSTOM_COMPONENTS] = {DOMAIN: integration}
    hass.config.components.add(DOMAIN)

    assert await async_setup_component(hass, "application_credentials", {})
    await hass.async_block_till_done()
