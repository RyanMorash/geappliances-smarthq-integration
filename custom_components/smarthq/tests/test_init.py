"""Test the SmartHQ init."""
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.smarthq import async_setup


async def test_async_setup_does_not_register_placeholder_oauth(hass: HomeAssistant) -> None:
    """async_setup must not register a placeholder Local OAuth client."""
    with patch(
        "homeassistant.helpers.config_entry_oauth2_flow.async_register_implementation"
    ) as mock_register:
        assert await async_setup(hass, {}) is True
        mock_register.assert_not_called()
