"""Test the SmartHQ config flow."""
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthq.const import DOMAIN


@pytest.fixture
def mock_setup_entry():
    """Mock setting up a config entry."""
    with patch(
        "custom_components.smarthq.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


async def test_config_flow_oauth(
    hass: HomeAssistant, load_smarthq_integration, mock_setup_entry
):
    """OAuth flow requires Application Credentials (no placeholder client)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "missing_credentials"


async def test_config_flow_abort_if_already_setup(
    hass: HomeAssistant, load_smarthq_integration
):
    """Test we abort if SmartHQ is already setup."""
    MockConfigEntry(
        domain=DOMAIN,
        title="SmartHQ",
        data={},
        source=config_entries.SOURCE_USER,
        unique_id="smarthq_oauth",
    ).add_to_hass(hass)

    with patch(
        "homeassistant.config_entries._support_single_config_entry_only",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
