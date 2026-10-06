"""Tests for SmartHQ switch entity discovery."""

from unittest.mock import MagicMock

import pytest

from custom_components.smarthq.const import DOMAIN
from custom_components.smarthq.switch import async_setup_entry

pytestmark = pytest.mark.asyncio

_TOGGLE = "cloud.smarthq.service.toggle"
_LIGHT = "cloud.smarthq.domain.light"


def _toggle(service_id: str, service_device_type: str) -> dict:
    return {
        "serviceId": service_id,
        "serviceType": _TOGGLE,
        "domainType": _LIGHT,
        "serviceDeviceType": service_device_type,
        "supportedCommands": ["cloud.smarthq.command.toggle.set"],
    }


async def _setup_switches(services: list[dict]) -> list:
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry-1"
    coordinator = MagicMock()
    coordinator.data = {
        "dev-1": {
            "item": {"services": services},
            "settings": [],
            "info": {"nickname": "Smoker"},
        }
    }
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                "client": MagicMock(),
                "coordinator": coordinator,
            }
        }
    }
    added: list = []

    def _add(entities, update_before_add=False) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add)
    return added


async def test_distinct_service_device_types_get_their_own_switches() -> None:
    """Same service type and domain with different serviceDeviceType values are separate entities."""
    entities = await _setup_switches(
        [
            _toggle("svc-smoker", "cloud.smarthq.device.smoker"),
            _toggle("svc-light", "cloud.smarthq.device.light"),
        ]
    )

    assert [entity._service_id for entity in entities] == ["svc-smoker", "svc-light"]


async def test_exact_service_tuple_is_still_deduped() -> None:
    """A repeated (serviceType, domainType, serviceDeviceType) does not create a second switch."""
    entities = await _setup_switches(
        [
            _toggle("svc-1", "cloud.smarthq.device.smoker"),
            _toggle("svc-2", "cloud.smarthq.device.smoker"),
        ]
    )

    assert [entity._service_id for entity in entities] == ["svc-1"]
