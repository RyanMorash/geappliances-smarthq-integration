"""Switch platform for SmartHQ integration.

Entity registration is driven by:
  coordinator.data[device_id]["item"]["services"]  (WS-based service switches)

Service → entity mapping (via SERVICE_MAPPING allowlist):
  toggle              + CMD_TOGGLE_SET                               → SmartHQToggleSwitch
  mode                + CMD_MODE_SET + domain in SWITCH_MODE_DOMAINS → SmartHQModeSwitch
  laundry.toggle.v2   + CMD_LAUNDRY_TOGGLE_V2_SET                   → SmartHQLaundryToggleSwitch

BOOLEAN notification rules are exposed as read-only binary sensors in binary_sensor.py.

ServiceTypes NOT in SERVICE_MAPPING are silently ignored (allowlist approach).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER, DEFAULT_NAME, sdev_prefix
from .dispatcher import SIGNAL_DEVICE_UPDATED
from .service_registry import (
    TOGGLE_SERVICE,
    MODE_SERVICE,
    LAUNDRY_TOGGLE_V2_SERVICE,
    CMD_TOGGLE_SET,
    CMD_MODE_SET,
    CMD_LAUNDRY_TOGGLE_V2_SET,
    SWITCH_MODE_DOMAINS,
    make_unique_id,
    get_service_mapping,
    is_platform_mapped,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def _bucket(hass, entry):
    return hass.data.get(DOMAIN, {}).get(entry.entry_id) or {}

def _store(hass, entry):
    return _bucket(hass, entry).get("store") or {}

def _dev_payload(hass, entry, device_id):
    return _store(hass, entry).get(device_id) or {}

def _snapshot_for(hass, entry, device_id):
    return _dev_payload(hass, entry, device_id).get("snapshot") or {}

def _device_info_for(hass, entry, device_id):
    info = _dev_payload(hass, entry, device_id).get("info") or {}
    name = info.get("nickname") or info.get("name") or DEFAULT_NAME
    model = info.get("model") or info.get("deviceType") or ""
    sw_version = info.get("firmwareRevision") or ""
    return {
        "identifiers": {(DOMAIN, device_id)},
        "manufacturer": MANUFACTURER,
        "name": name,
        "model": model,
        "sw_version": sw_version,
    }

def _label_for_toggle(dom, sdev: str = ""):
    d = dom.lower()
    if "warm.auto" in d:
        return "Auto Warm", "mdi:fire-alert"
    if "lock" in d or "override" in d:
        return "Control Lock", "mdi:lock"
    if "light" in d or "cavity" in d:
        return "Cavity Light", "mdi:lightbulb"
    if "smoke" in d:
        return "Clear Smoke", "mdi:smoke"
    base = dom.split(".")[-1].replace("_", " ").title() if dom else "Toggle"
    prefix = sdev_prefix(sdev)
    return (f"{prefix} {base}".strip() if prefix else base), "mdi:toggle-switch"

def _label_for_mode_switch(dom, sdev: str = ""):
    d = dom.lower()
    if "brightness" in d or "light" in d:
        return "Cavity Light", "mdi:lightbulb"
    if "lock" in d or "override" in d:
        return "Control Lock", "mdi:lock"
    base = dom.split(".")[-1].replace("_", " ").title() if dom else "Mode"
    prefix = sdev_prefix(sdev)
    return (f"{prefix} {base}".strip() if prefix else base), "mdi:toggle-switch"


def _pretty_dom(dom: str) -> str:
    """Return human-readable name from domain tail."""
    return dom.split(".")[-1].replace("_", " ").title() if dom else ""


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up SmartHQ switches from coordinator service definitions."""
    bucket = _bucket(hass, entry)
    ws = bucket.get("client") or bucket.get("ws")
    coordinator = bucket.get("coordinator")

    if not ws:
        _LOGGER.error("[SWITCH] WebSocket client not found in bucket")
        return
    if not coordinator or not coordinator.data:
        _LOGGER.warning("[SWITCH] Coordinator data not available yet")
        return

    entities = []

    for device_id, device_item in coordinator.data.items():
        item = device_item.get("item") or {}
        services_list = item.get("services") or []
        if not isinstance(services_list, list):
            continue

        # Per-device dedup: one switch per (serviceType, domainType, serviceDeviceType).
        # The same type and domain with a different serviceDeviceType is another
        # component and gets its own entity. Labels already include that value.
        seen_switch_domains: set[tuple[str, str, str]] = set()

        for svc in services_list:
            if not isinstance(svc, dict):
                continue

            stype = svc.get("serviceType") or ""
            dom = svc.get("domainType") or ""
            service_id = svc.get("id") or svc.get("serviceId") or ""
            cmds = svc.get("supportedCommands") or []

            # ── Allowlist check: skip serviceTypes not in SERVICE_MAPPING ──
            if get_service_mapping(stype) is None:
                _LOGGER.debug("[SWITCH] Skipping unmapped serviceType=%s svc=%s", stype, service_id)
                continue

            # ── Skip serviceTypes not mapped to switch platform ──
            if not is_platform_mapped(stype, "switch"):
                continue

            # ── Route to entity builder based on serviceType ──
            if stype == TOGGLE_SERVICE and CMD_TOGGLE_SET in cmds:
                # Skip only an exact tuple duplicate. A different serviceDeviceType
                # is a separate switch (for example smoker cavity light vs light).
                sdev = str(svc.get("serviceDeviceType") or "")
                dedup_key = (stype, dom, sdev)
                if dedup_key in seen_switch_domains:
                    _LOGGER.debug(
                        "[SWITCH] Skipping duplicate toggle switch for device=%s domain=%s serviceDeviceType=%s svc=%s",
                        device_id, dom, sdev, service_id,
                    )
                    continue
                seen_switch_domains.add(dedup_key)
                label, icon = _label_for_toggle(dom, sdev)
                entities.append(SmartHQToggleSwitch(
                    hass=hass, entry=entry, ws=ws,
                    device_id=device_id, service_id=service_id,
                    label=label, icon=icon,
                    unique_id=make_unique_id(device_id, service_id, "toggle"),
                ))

            elif stype == MODE_SERVICE and CMD_MODE_SET in cmds and dom in SWITCH_MODE_DOMAINS:
                # Skip only an exact tuple duplicate. A different serviceDeviceType
                # is a separate switch.
                sdev = str(svc.get("serviceDeviceType") or "")
                dedup_key = (stype, dom, sdev)
                if dedup_key in seen_switch_domains:
                    _LOGGER.debug(
                        "[SWITCH] Skipping duplicate mode switch for device=%s domain=%s serviceDeviceType=%s svc=%s",
                        device_id, dom, sdev, service_id,
                    )
                    continue
                seen_switch_domains.add(dedup_key)
                label, icon = _label_for_mode_switch(dom, sdev)
                entities.append(SmartHQModeSwitch(
                    hass=hass, entry=entry, ws=ws,
                    device_id=device_id, service_id=service_id,
                    label=label, icon=icon,
                    unique_id=make_unique_id(device_id, service_id, "mode_switch"),
                ))

            elif stype == LAUNDRY_TOGGLE_V2_SERVICE and CMD_LAUNDRY_TOGGLE_V2_SET in cmds:
                label = _pretty_dom(dom) or "Washer Link"
                entities.append(SmartHQLaundryToggleSwitch(
                    hass=hass, entry=entry, ws=ws,
                    device_id=device_id, service_id=service_id,
                    label=label, dom=dom,
                    unique_id=make_unique_id(device_id, service_id, "laundry_toggle_v2"),
                ))

    _LOGGER.info("[SWITCH] Registering %d switch entities", len(entities))
    if entities:
        async_add_entities(entities, update_before_add=False)


# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------

class _SmartHQSwitchBase(SwitchEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hass, entry, ws, device_id, service_id, label, icon, unique_id):
        self.hass = hass
        self._entry = entry
        self._ws = ws
        self._device_id = device_id
        self._service_id = service_id
        info = _dev_payload(hass, entry, device_id).get("info") or {}
        dev_name = info.get("nickname") or info.get("name") or DEFAULT_NAME
        self._attr_name = f"{dev_name} {label}"
        self._attr_icon = icon
        self._attr_unique_id = unique_id

    def _get_service_state(self):
        snap = _snapshot_for(self.hass, self._entry, self._device_id)
        return (snap.get("services") or {}).get(self._service_id) or {}

    def _find_cooking_state(self):
        snap = _snapshot_for(self.hass, self._entry, self._device_id)
        for st in (snap.get("services") or {}).values():
            if isinstance(st, dict) and "cooking" in str(st.get("serviceType") or ""):
                return st
        return {}

    @property
    def available(self):
        st = self._get_service_state()
        if not st or st.get("disabled"):
            return False
        dev_data = _dev_payload(self.hass, self._entry, self._device_id)
        if (dev_data.get("presence") or {}).get("presence") != "ONLINE":
            return False
        return True

    @property
    def device_info(self):
        return _device_info_for(self.hass, self._entry, self._device_id)

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DEVICE_UPDATED.format(device_id=self._device_id),
                self._signal_update,
            )
        )
        self._signal_update()

    @callback
    def _signal_update(self):
        self.async_write_ha_state()


class SmartHQToggleSwitch(_SmartHQSwitchBase):
    """Switch for a toggle service (on/off via toggle.set)."""

    @property
    def is_on(self):
        st = self._get_service_state()
        value = st.get("on")
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value == 1
        if isinstance(value, str):
            return value.lower() in ("on", "true", "1")
        return False

    async def async_turn_on(self, **kwargs):
        _LOGGER.info("[TOGGLE] ON: %s", self._attr_name)
        await self._ws.async_set_toggle(device_id=self._device_id, service_id=self._service_id, on=True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        _LOGGER.info("[TOGGLE] OFF: %s", self._attr_name)
        await self._ws.async_set_toggle(device_id=self._device_id, service_id=self._service_id, on=False)
        self._attr_is_on = False
        self.async_write_ha_state()


class SmartHQModeSwitch(_SmartHQSwitchBase):
    """Switch for a mode service whose domain is binary (e.g. cavity light, lock)."""

    _ON_SUFFIXES = frozenset({"on", "high", "dim", "enabled", "locked"})

    @property
    def is_on(self):
        st = self._get_service_state()
        if "on" in st:
            return bool(st["on"])
        mode = st.get("mode")
        if not mode:
            return False
        return str(mode).split(".")[-1].lower() in self._ON_SUFFIXES

    @property
    def available(self):
        if not super().available:
            return False
        # Light/brightness domains require device to be active
        name_lower = self._attr_name.lower()
        if "light" in name_lower or "cavity" in name_lower:
            run_status = str(self._find_cooking_state().get("runStatus") or "").lower()
            if run_status and "off" in run_status:
                return False
        return True

    async def async_turn_on(self, **kwargs):
        _LOGGER.info("[MODE_SWITCH] ON: %s", self._attr_name)
        try:
            await self._ws.async_set_mode(self._device_id, self._service_id, "cloud.smarthq.type.mode.on")
            self.async_write_ha_state()
        except Exception as exc:
            _LOGGER.error("[MODE_SWITCH] Error ON %s: %s", self._attr_name, exc)

    async def async_turn_off(self, **kwargs):
        _LOGGER.info("[MODE_SWITCH] OFF: %s", self._attr_name)
        try:
            await self._ws.async_set_mode(self._device_id, self._service_id, "cloud.smarthq.type.mode.off")
            self.async_write_ha_state()
        except Exception as exc:
            _LOGGER.error("[MODE_SWITCH] Error OFF %s: %s", self._attr_name, exc)


# ---------------------------------------------------------------------------
# Laundry Toggle v2 switch
# ---------------------------------------------------------------------------

class SmartHQLaundryToggleSwitch(_SmartHQSwitchBase):
    """Switch for laundry.toggle.v2 service (e.g. Washer Link dryer)."""

    def __init__(self, hass, entry, ws, device_id, service_id, label, dom, unique_id):
        super().__init__(
            hass=hass, entry=entry, ws=ws,
            device_id=device_id, service_id=service_id,
            label=label, icon="mdi:washing-machine",
            unique_id=unique_id,
        )
        self._dom = dom

    @property
    def is_on(self) -> bool:
        return bool(self._get_service_state().get("on", False))

    @property
    def extra_state_attributes(self) -> dict:
        st = self._get_service_state()
        cycle = st.get("cycle")
        return {"cycle": cycle.split(".")[-1].upper() if cycle else None}

    async def async_turn_on(self, **kwargs):
        _LOGGER.info("[LAUNDRY_TOGGLE_V2] ON: %s", self._attr_name)
        await self._ws.async_set_laundry_toggle_v2(
            device_id=self._device_id, service_id=self._service_id, on=True
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        _LOGGER.info("[LAUNDRY_TOGGLE_V2] OFF: %s", self._attr_name)
        await self._ws.async_set_laundry_toggle_v2(
            device_id=self._device_id, service_id=self._service_id, on=False
        )
        self.async_write_ha_state()
