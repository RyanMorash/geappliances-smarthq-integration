"""Tests for the SmartHQ binary_sensor platform."""

from unittest.mock import MagicMock

from custom_components.smarthq.binary_sensor import (
    SmartHQDoorBinarySensor,
    SmartHQFilterBinarySensor,
)
from custom_components.smarthq.const import DOMAIN

DEVICE_ID = "test-device"
DOOR_SERVICE_ID = "test-door"
FILTER_SERVICE_ID = "test-filter"


def _store_for(service_id: str, state: dict) -> dict:
    return {
        DOMAIN: {
            "test-entry": {
                "store": {
                    DEVICE_ID: {
                        "snapshot": {"services": {service_id: state}},
                    }
                }
            }
        }
    }


def _create_door_entity(state: dict) -> SmartHQDoorBinarySensor:
    hass = MagicMock()
    hass.data = _store_for(DOOR_SERVICE_ID, state)
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return SmartHQDoorBinarySensor(
        hass, entry, DEVICE_ID, DOOR_SERVICE_ID, "Door", "test-door-uid"
    )


def _create_filter_entity(state: dict) -> SmartHQFilterBinarySensor:
    hass = MagicMock()
    hass.data = _store_for(FILTER_SERVICE_ID, state)
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return SmartHQFilterBinarySensor(
        hass, entry, DEVICE_ID, FILTER_SERVICE_ID, "Filter", "test-filter-uid"
    )


def test_door_single_open_flag():
    """singleOpen=True reports the door as open."""
    assert _create_door_entity({"singleOpen": True}).is_on is True


def test_door_left_open_flag():
    """leftOpen=True reports the door as open."""
    assert _create_door_entity({"leftOpen": True}).is_on is True


def test_door_right_open_flag():
    """rightOpen=True reports the door as open."""
    assert _create_door_entity({"rightOpen": True}).is_on is True


def test_door_open_when_any_flag_true():
    """Any *Open flag being true is enough to report open."""
    assert _create_door_entity({"leftOpen": False, "rightOpen": True}).is_on is True


def test_door_closed_when_open_flags_false():
    """All *Open flags false means closed."""
    assert _create_door_entity({"singleOpen": False, "leftOpen": False}).is_on is False


def test_door_ignores_legacy_door_state_keys():
    """Legacy doorState/state/open/on must not drive is_on."""
    assert _create_door_entity({"doorState": "open"}).is_on is False
    assert _create_door_entity({"state": "open", "open": True, "on": True}).is_on is False


def test_door_unavailable_when_service_missing():
    """An entity with no service state in the store is unavailable."""
    entity = _create_door_entity({})
    assert entity.available is False


def test_filter_on_when_expired():
    """Filter binary sensor is on when expired is true."""
    assert _create_filter_entity({"expired": True}).is_on is True


def test_filter_off_when_not_expired():
    """Filter binary sensor is off when expired is false."""
    assert _create_filter_entity({"expired": False}).is_on is False


def test_filter_ignores_legacy_status_keys():
    """filterStatus/replacementNeeded/lifeRemaining must not drive is_on."""
    assert _create_filter_entity(
        {
            "filterStatus": "replace",
            "replacementNeeded": True,
            "lifeRemaining": 0,
            "expired": False,
        }
    ).is_on is False


def test_filter_extra_state_attributes():
    """usagePercent and expirationElapsedTime are exposed as attributes."""
    entity = _create_filter_entity(
        {"expired": False, "usagePercent": 42, "expirationElapsedTime": 86400}
    )
    assert entity.extra_state_attributes == {
        "usagePercent": 42,
        "expirationElapsedTime": 86400,
    }
