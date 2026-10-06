"""Tests for the SmartHQ binary_sensor platform."""

from unittest.mock import MagicMock

from custom_components.smarthq.binary_sensor import (
    SmartHQDoorBinarySensor,
<<<<<<< HEAD
    SmartHQFilterBinarySensor,
=======
    SmartHQNotificationRuleBinarySensor,
>>>>>>> 9594867 (Expose notification rules as read-only binary sensors)
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


def _create_door_entity(state: dict, *, toggle_backed: bool = False) -> SmartHQDoorBinarySensor:
    hass = MagicMock()
    hass.data = _store_for(DOOR_SERVICE_ID, state)
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return SmartHQDoorBinarySensor(
        hass,
        entry,
        DEVICE_ID,
        DOOR_SERVICE_ID,
        "Door",
        "test-door-uid",
        toggle_backed=toggle_backed,
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
    """Legacy doorState/state/open/on must not drive is_on on DOOR_SERVICE."""
    assert _create_door_entity({"doorState": "open"}).is_on is False
    assert _create_door_entity({"state": "open", "open": True, "on": True}).is_on is False


def test_toggle_backed_door_open():
    """A read-only toggle on DOOR_DOMAIN reports open via on=True."""
    assert _create_door_entity({"on": True}, toggle_backed=True).is_on is True


def test_toggle_backed_door_closed():
    """A read-only toggle on DOOR_DOMAIN reports closed via on=False."""
    assert _create_door_entity({"on": False}, toggle_backed=True).is_on is False


def test_door_unavailable_when_service_missing():
    """An entity with no service state in the store is unavailable."""
    entity = _create_door_entity({})
    assert entity.available is False


<<<<<<< HEAD
def test_door_unavailable_without_open_flags():
    """DOOR_SERVICE is unavailable when no boolean *Open field is present."""
    assert _create_door_entity({"doorState": "open"}).available is False


def test_door_available_when_open_flag_present():
    """DOOR_SERVICE is available when at least one boolean *Open field exists."""
    assert _create_door_entity({"singleOpen": False}).available is True


def test_toggle_backed_door_available_when_on_bool():
    """Toggle-backed door is available when on is a bool."""
    assert _create_door_entity({"on": False}, toggle_backed=True).available is True


def test_toggle_backed_door_unavailable_without_on():
    """Toggle-backed door is unavailable without a boolean on field."""
    assert _create_door_entity({}, toggle_backed=True).available is False


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


def test_filter_unavailable_without_expired():
    """Filter is unavailable when expired is missing."""
    assert _create_filter_entity({"usagePercent": 50}).available is False


def test_filter_available_when_expired_present():
    """Filter is available when expired is a bool."""
    assert _create_filter_entity({"expired": False}).available is True
=======
RULE_ID = "door-alert-rule"


def _create_notification_rule_entity(
    settings: list,
    initial_value: bool = False,
) -> SmartHQNotificationRuleBinarySensor:
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "test-entry": {
                "store": {
                    DEVICE_ID: {
                        "settings": settings,
                    }
                }
            }
        }
    }
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return SmartHQNotificationRuleBinarySensor(
        hass,
        entry,
        DEVICE_ID,
        RULE_ID,
        "Smoker",
        "Door Alert",
        "Notify when door opens",
        initial_value,
        "test-notification-rule-uid",
    )


def test_notification_rule_on_from_store_current():
    """BOOLEAN rule with current=True reports is_on."""
    entity = _create_notification_rule_entity(
        [{"id": RULE_ID, "type": "BOOLEAN", "current": True}],
    )
    assert entity.is_on is True


def test_notification_rule_off_from_store_current():
    """BOOLEAN rule with current=False reports off."""
    entity = _create_notification_rule_entity(
        [{"id": RULE_ID, "type": "BOOLEAN", "current": False}],
        initial_value=True,
    )
    assert entity.is_on is False


def test_notification_rule_falls_back_to_initial_value():
    """When the rule is missing from the store, use the value from setup."""
    entity = _create_notification_rule_entity([], initial_value=True)
    assert entity.is_on is True
>>>>>>> 9594867 (Expose notification rules as read-only binary sensors)
