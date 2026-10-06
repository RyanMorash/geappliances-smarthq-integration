"""Tests for SmartHQ WebSocket state handling."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

pytestmark = pytest.mark.asyncio

from custom_components.smarthq.service_registry import POWER_USAGE_SERVICE, THERMOSTAT_SERVICE
from custom_components.smarthq.ws_client import (
    PING_IDLE_SECONDS,
    SmartHQWebsocket,
    _presence_from_payload,
    _redact_access_token_from_text,
    _strip_access_token_from_url,
    _ws_endpoint_host_for_log,
)


@pytest.mark.parametrize(("thermostat_on", "expected_power"), [(False, 0), (True, 708)])
async def test_thermostat_update_reconciles_instantaneous_power(thermostat_on: bool, expected_power: int) -> None:
    """Clear stale power only when the thermostat explicitly turns off."""
    device_id = "test-device"
    store = {
        device_id: {
            "snapshot": {
                "services": {
                    "power": {
                        "serviceType": POWER_USAGE_SERVICE,
                        "instantaneousPower": 708,
                    }
                }
            }
        }
    }
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)
    payload = {
        "kind": "pubsub#service",
        "deviceId": device_id,
        "serviceId": "thermostat",
        "serviceType": THERMOSTAT_SERVICE,
        "domainType": "cloud.smarthq.domain.thermostat",
        "state": {"on": thermostat_on},
    }

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(payload)

    assert store[device_id]["snapshot"]["services"]["power"]["instantaneousPower"] == expected_power


async def test_idle_ping_sent_without_inbound_traffic() -> None:
    """JSON keepalive ping must fire when idle longer than PING_IDLE_SECONDS."""
    device_id = "device-1"
    store = {device_id: {"snapshot": {"services": {}, "index": {}, "raw": {}}}}
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)

    ws = AsyncMock()
    ws.closed = False

    updated = await websocket._maybe_send_idle_ping(
        ws,
        last_activity=0.0,
        now=float(PING_IDLE_SECONDS),
    )

    assert updated == float(PING_IDLE_SECONDS)
    ws.send_json.assert_awaited_once_with({"kind": "websocket#ping", "action": "ping"})


async def test_connect_log_uses_host_without_access_token(caplog: pytest.LogCaptureFixture) -> None:
    """Connection logs must not include websocket access_token query values."""
    endpoint = (
        "wss://ws.example.com/v2/subscribe?access_token=secret-token&user=abc"
    )
    api = MagicMock()
    api.async_get_websocket_endpoint = AsyncMock(return_value=endpoint)
    api._oauth_session = AsyncMock(
        return_value=MagicMock(
            async_ensure_token_valid=AsyncMock(),
            token={"expires_at": 9_999_999_999},
        )
    )

    session = MagicMock()
    ws_context = AsyncMock()
    ws_context.__aenter__ = AsyncMock(return_value=AsyncMock(closed=False))
    ws_context.__aexit__ = AsyncMock(return_value=None)
    session.ws_connect = MagicMock(return_value=ws_context)

    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[], store={})
    websocket._session = session

    async def stop_after_backoff(_seconds: float) -> None:
        websocket._stopped.set()

    with caplog.at_level(logging.INFO, logger="custom_components.smarthq.ws_client"):
        with patch.object(websocket, "_subscribe_all", AsyncMock()):
            with patch.object(
                websocket,
                "_run_connected_session",
                AsyncMock(side_effect=aiohttp.ClientError("closed")),
            ):
                with patch.object(websocket, "_refetch_devices_after_disconnect", AsyncMock()):
                    with patch(
                        "custom_components.smarthq.ws_client.asyncio.sleep",
                        AsyncMock(side_effect=stop_after_backoff),
                    ):
                        await websocket._runner()

    assert _ws_endpoint_host_for_log(_strip_access_token_from_url(endpoint)) == "ws.example.com"
    assert "secret-token" not in caplog.text
    assert "Connecting SmartHQ WS -> ws.example.com" in caplog.text


async def test_presence_string_stored_under_presence_key() -> None:
    """String presence payloads are normalized to presence['presence']."""
    device_id = "presence-device"
    store = {device_id: {}}
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)
    payload = {"deviceId": device_id, "presence": "online"}

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(payload)

    assert store[device_id]["presence"] == {"presence": "ONLINE"}


async def test_presence_dict_uppercases_presence_value() -> None:
    """Dict presence payloads must uppercase presence for command gating."""
    assert _presence_from_payload({"presence": "online"}) == {"presence": "ONLINE"}


async def test_redact_access_token_from_text() -> None:
    """Exception strings must not retain websocket access_token values."""
    raw = (
        "400, message='Bad Request', url='wss://ws.example.com/v2?"
        "access_token=secret-token&user=abc'"
    )
    redacted = _redact_access_token_from_text(raw)
    assert "secret-token" not in redacted
    assert "access_token=***" in redacted


async def test_handshake_failure_logs_redacted_token(caplog: pytest.LogCaptureFixture) -> None:
    """WS handshake errors must not log access_token from aiohttp exception text."""
    endpoint = "wss://ws.example.com/v2?access_token=secret-token"
    api = MagicMock()
    api.async_get_websocket_endpoint = AsyncMock(return_value=endpoint)

    session = MagicMock()
    session.ws_connect = MagicMock(
        side_effect=aiohttp.ClientError(
            f"Connection failed, url={endpoint}",
        )
    )

    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=["dev-1"], store={"dev-1": {}})
    websocket._session = session

    async def stop_after_backoff(_seconds: float) -> None:
        websocket._stopped.set()

    with caplog.at_level(logging.WARNING, logger="custom_components.smarthq.ws_client"):
        with patch.object(websocket, "_refetch_devices_after_disconnect", AsyncMock()) as mock_refetch:
            with patch(
                "custom_components.smarthq.ws_client.asyncio.sleep",
                AsyncMock(side_effect=stop_after_backoff),
            ):
                await websocket._runner()

    mock_refetch.assert_not_awaited()
    assert "secret-token" not in caplog.text
    assert "access_token=***" in caplog.text


async def test_disconnect_refetch_preserves_service_metadata() -> None:
    """REST snapshot refresh keeps label, name, and config like bootstrap."""
    device_id = "refetch-device"
    store = {device_id: {"snapshot": {"services": {}, "index": {}, "raw": {}}}}
    api = MagicMock()
    api.async_get_device_item = AsyncMock(
        return_value={
            "services": [
                {
                    "serviceId": "svc-1",
                    "serviceType": "cloud.smarthq.service.mode",
                    "domainType": "cloud.smarthq.domain.example",
                    "serviceDeviceType": "cloud.smarthq.device.test",
                    "label": "Mode",
                    "name": "Example Mode",
                    "config": {"options": ["a", "b"]},
                    "state": {"mode": "cloud.smarthq.type.example.a"},
                }
            ]
        }
    )
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._refetch_devices_after_disconnect()

    service = store[device_id]["snapshot"]["services"]["svc-1"]
    assert service["label"] == "Mode"
    assert service["name"] == "Example Mode"
    assert service["config"] == {"options": ["a", "b"]}


async def test_runner_refetches_only_after_connected_session() -> None:
    """Failed handshake must not trigger per-device REST refetch storms."""
    endpoint = "wss://ws.example.com/v2?access_token=secret"
    api = MagicMock()
    api.async_get_websocket_endpoint = AsyncMock(return_value=endpoint)
    api._oauth_session = AsyncMock(
        return_value=MagicMock(
            async_ensure_token_valid=AsyncMock(),
            token={"expires_at": 9_999_999_999},
        )
    )

    session = MagicMock()
    ws_context = AsyncMock()
    ws_context.__aenter__ = AsyncMock(return_value=AsyncMock(closed=False))
    ws_context.__aexit__ = AsyncMock(return_value=None)
    session.ws_connect = MagicMock(return_value=ws_context)

    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=["dev-1"], store={"dev-1": {}})
    websocket._session = session

    async def stop_after_refetch() -> None:
        websocket._stopped.set()

    with patch.object(websocket, "_subscribe_all", AsyncMock()):
        with patch.object(
            websocket,
            "_run_connected_session",
            AsyncMock(side_effect=aiohttp.ClientError("closed")),
        ):
            with patch.object(
                websocket,
                "_refetch_devices_after_disconnect",
                AsyncMock(side_effect=stop_after_refetch),
            ) as mock_refetch:
                with patch(
                    "custom_components.smarthq.ws_client.asyncio.sleep",
                    AsyncMock(return_value=None),
                ):
                    await websocket._runner()

    mock_refetch.assert_awaited_once()


async def test_disconnect_refetches_device_snapshot() -> None:
    """When the socket drops, known devices are refreshed via REST."""
    device_id = "refetch-device"
    store = {
        device_id: {
            "snapshot": {
                "services": {"old": {"on": False}},
                "index": {},
                "raw": {},
            }
        }
    }
    api = MagicMock()
    api.async_get_device_item = AsyncMock(
        return_value={
            "services": [
                {
                    "serviceId": "svc-1",
                    "serviceType": "cloud.smarthq.service.toggle",
                    "domainType": "cloud.smarthq.domain.light",
                    "serviceDeviceType": "cloud.smarthq.device.test",
                    "state": {"on": True},
                }
            ]
        }
    )
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send") as mock_dispatch:
        await websocket._refetch_devices_after_disconnect()

    api.async_get_device_item.assert_awaited_once_with(device_id)
    assert store[device_id]["snapshot"]["services"]["svc-1"]["on"] is True
    mock_dispatch.assert_called_once()


def _controllable_store(
    *,
    service_type: str = "cloud.smarthq.service.toggle",
) -> tuple[str, str, dict]:
    """Online device whose snapshot must not change until a service event."""
    device_id = "device-abcdef12"
    service_id = "service-abcdef12"
    store = {
        device_id: {
            "presence": {"presence": "ONLINE"},
            "snapshot": {
                "services": {
                    service_id: {
                        "on": False,
                        "mode": "cloud.smarthq.type.mode.off",
                        "serviceType": service_type,
                        "domainType": "cloud.smarthq.domain.light",
                        "serviceDeviceType": "cloud.smarthq.device.light",
                    }
                },
                "index": {},
                "raw": {},
            },
        }
    }
    return device_id, service_id, store


async def _wait_for_command_recoveries(websocket: SmartHQWebsocket) -> None:
    """Let scheduled command-failure refetches finish."""
    tasks = list(websocket._recovery_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def _command_event(device_id: str, service_id: str, outcome: str) -> dict:
    """pubsub#command that also carries service fields a state update might misuse."""
    return {
        "kind": "pubsub#command",
        "deviceId": device_id,
        "serviceId": service_id,
        "serviceType": "cloud.smarthq.service.toggle",
        "domainType": "cloud.smarthq.domain.light",
        "serviceDeviceType": "cloud.smarthq.device.light",
        "state": {"on": True, "mode": "cloud.smarthq.type.mode.on"},
        "command": {"commandType": "cloud.smarthq.command.toggle.set", "on": True},
        "correlationId": "52b52c43-35dd-4d5a-8195-2d7dc316e080",
        "outcome": outcome,
    }


async def test_set_toggle_does_not_write_local_snapshot() -> None:
    """Toggle commands wait for pubsub#service instead of writing on locally."""
    device_id, service_id, store = _controllable_store()
    api = MagicMock()
    api.async_send_command = AsyncMock(
        return_value={
            "correlationId": "corr-toggle",
            "outcome": "cloud.smarthq.outcome.success",
        }
    )
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    await websocket.async_set_toggle(device_id, service_id, True)

    service = store[device_id]["snapshot"]["services"][service_id]
    assert service["on"] is False
    assert service["mode"] == "cloud.smarthq.type.mode.off"
    api.async_send_command.assert_awaited_once()


async def test_set_mode_does_not_write_local_snapshot() -> None:
    """Mode commands wait for pubsub#service instead of writing mode or on locally."""
    device_id, service_id, store = _controllable_store(service_type="cloud.smarthq.service.mode")
    api = MagicMock()
    api.async_send_command = AsyncMock(
        return_value={
            "correlationId": "corr-mode",
            "outcome": "cloud.smarthq.outcome.success",
        }
    )
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    await websocket.async_set_mode(device_id, service_id, "cloud.smarthq.type.mode.on")

    service = store[device_id]["snapshot"]["services"][service_id]
    assert service["on"] is False
    assert service["mode"] == "cloud.smarthq.type.mode.off"
    api.async_send_command.assert_awaited_once()


async def test_service_event_is_what_updates_toggle_state() -> None:
    """Entity state changes when pubsub#service arrives."""
    device_id, service_id, store = _controllable_store()
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)
    payload = {
        "kind": "pubsub#service",
        "deviceId": device_id,
        "serviceId": service_id,
        "serviceType": "cloud.smarthq.service.toggle",
        "domainType": "cloud.smarthq.domain.light",
        "state": {"on": True},
    }

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(payload)

    assert store[device_id]["snapshot"]["services"][service_id]["on"] is True


async def test_command_success_does_not_change_entity_state() -> None:
    """A success outcome waits for pubsub#service and does not refetch."""
    device_id, service_id, store = _controllable_store()
    api = MagicMock()
    api.async_get_device_item = AsyncMock()
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send") as mock_dispatch:
        await websocket._on_message(
            _command_event(device_id, service_id, "cloud.smarthq.outcome.success")
        )

    service = store[device_id]["snapshot"]["services"][service_id]
    assert service["on"] is False
    assert service["mode"] == "cloud.smarthq.type.mode.off"
    api.async_get_device_item.assert_not_called()
    mock_dispatch.assert_not_called()


@pytest.mark.parametrize(
    "outcome",
    [
        "cloud.smarthq.outcome.failed",
        "cloud.smarthq.outcome.timeout",
        "cloud.smarthq.outcome.unknown",
    ],
)
async def test_failed_command_refetches_device_instead_of_applying_event(outcome: str) -> None:
    """failed, timeout, and unknown outcomes reload the device and do not use the event as state."""
    device_id, service_id, store = _controllable_store()
    api = MagicMock()
    api.async_get_device_item = AsyncMock(
        return_value={
            "services": [
                {
                    "serviceId": service_id,
                    "serviceType": "cloud.smarthq.service.toggle",
                    "domainType": "cloud.smarthq.domain.light",
                    "serviceDeviceType": "cloud.smarthq.device.light",
                    "state": {"on": False, "mode": "cloud.smarthq.type.mode.off"},
                }
            ]
        }
    )
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send") as mock_dispatch:
        await websocket._on_message(_command_event(device_id, service_id, outcome))
        await _wait_for_command_recoveries(websocket)

    api.async_get_device_item.assert_awaited_once_with(device_id)
    service = store[device_id]["snapshot"]["services"][service_id]
    assert service["on"] is False
    assert service["mode"] == "cloud.smarthq.type.mode.off"
    mock_dispatch.assert_called_once()


async def test_failed_command_does_not_apply_event_when_refetch_is_empty() -> None:
    """An empty refetch leaves the previous snapshot in place."""
    device_id, service_id, store = _controllable_store()
    api = MagicMock()
    api.async_get_device_item = AsyncMock(return_value={})
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send") as mock_dispatch:
        await websocket._on_message(
            _command_event(device_id, service_id, "cloud.smarthq.outcome.failed")
        )
        await _wait_for_command_recoveries(websocket)

    service = store[device_id]["snapshot"]["services"][service_id]
    assert service["on"] is False
    assert service["mode"] == "cloud.smarthq.type.mode.off"
    mock_dispatch.assert_not_called()


async def test_other_command_outcomes_do_not_refetch_or_update_state() -> None:
    """Outcomes outside failed/timeout/unknown still are not entity state."""
    device_id, service_id, store = _controllable_store()
    api = MagicMock()
    api.async_get_device_item = AsyncMock()
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send") as mock_dispatch:
        await websocket._on_message(
            _command_event(device_id, service_id, "cloud.smarthq.outcome.servicedisabled")
        )

    service = store[device_id]["snapshot"]["services"][service_id]
    assert service["on"] is False
    api.async_get_device_item.assert_not_called()
    mock_dispatch.assert_not_called()


def _device_item(service_id: str, *, on: bool) -> dict:
    return {
        "services": [
            {
                "serviceId": service_id,
                "serviceType": "cloud.smarthq.service.toggle",
                "domainType": "cloud.smarthq.domain.light",
                "serviceDeviceType": "cloud.smarthq.device.light",
                "state": {"on": on},
            }
        ]
    }


async def test_command_recovery_does_not_block_other_device_events() -> None:
    """A slow refetch for one device does not hold up another device's service event."""
    device_a, service_a, store = _controllable_store()
    device_b = "device-b-123456"
    service_b = "service-b-123456"
    store[device_b] = {
        "presence": {"presence": "ONLINE"},
        "snapshot": {
            "services": {
                service_b: {
                    "on": False,
                    "serviceType": "cloud.smarthq.service.toggle",
                    "domainType": "cloud.smarthq.domain.light",
                    "serviceDeviceType": "cloud.smarthq.device.light",
                }
            },
            "index": {},
            "raw": {},
        },
    }
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get(_device_id: str) -> dict:
        started.set()
        await release.wait()
        return _device_item(service_a, on=False)

    api = MagicMock()
    api.async_get_device_item = AsyncMock(side_effect=slow_get)
    websocket = SmartHQWebsocket(
        MagicMock(),
        api=api,
        device_ids=[device_a, device_b],
        store=store,
    )

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(
            _command_event(device_a, service_a, "cloud.smarthq.outcome.timeout")
        )
        await started.wait()
        await websocket._on_message(
            {
                "kind": "pubsub#service",
                "deviceId": device_b,
                "serviceId": service_b,
                "serviceType": "cloud.smarthq.service.toggle",
                "domainType": "cloud.smarthq.domain.light",
                "state": {"on": True},
            }
        )
        assert store[device_b]["snapshot"]["services"][service_b]["on"] is True
        assert store[device_a]["snapshot"]["services"][service_a]["on"] is False
        release.set()
        await _wait_for_command_recoveries(websocket)

    assert store[device_b]["snapshot"]["services"][service_b]["on"] is True


async def test_late_command_recovery_does_not_overwrite_newer_service_event() -> None:
    """REST recovery started before a service event must not replace that event."""
    device_id, service_id, store = _controllable_store()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get(_device_id: str) -> dict:
        started.set()
        await release.wait()
        return _device_item(service_id, on=False)

    api = MagicMock()
    api.async_get_device_item = AsyncMock(side_effect=slow_get)
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(
            _command_event(device_id, service_id, "cloud.smarthq.outcome.failed")
        )
        await started.wait()
        await websocket._on_message(
            {
                "kind": "pubsub#service",
                "deviceId": device_id,
                "serviceId": service_id,
                "serviceType": "cloud.smarthq.service.toggle",
                "domainType": "cloud.smarthq.domain.light",
                "state": {"on": True},
            }
        )
        release.set()
        await _wait_for_command_recoveries(websocket)

    assert store[device_id]["snapshot"]["services"][service_id]["on"] is True
    api.async_get_device_item.assert_awaited_once()


async def test_older_command_recovery_does_not_overwrite_newer_one() -> None:
    """Per-device recovery keeps the later REST snapshot when the first returns late."""
    device_id, service_id, store = _controllable_store()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def get_item(_device_id: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            return _device_item(service_id, on=False)
        return _device_item(service_id, on=True)

    api = MagicMock()
    api.async_get_device_item = AsyncMock(side_effect=get_item)
    websocket = SmartHQWebsocket(MagicMock(), api=api, device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(
            _command_event(device_id, service_id, "cloud.smarthq.outcome.unknown")
        )
        await first_started.wait()
        await websocket._on_message(
            _command_event(device_id, service_id, "cloud.smarthq.outcome.failed")
        )
        tasks = list(websocket._recovery_tasks)
        release_first.set()
        await asyncio.gather(*tasks)

    assert store[device_id]["snapshot"]["services"][service_id]["on"] is True
    assert calls == 2


async def test_smoke_level_socket_send_removed() -> None:
    """Smoke level is not sent as service#command on the websocket."""
    assert not hasattr(SmartHQWebsocket, "async_set_smoke_level")
    assert not hasattr(SmartHQWebsocket, "_optimistic_toggle_update")


_TOGGLE = "cloud.smarthq.service.toggle"
_LIGHT_DOMAIN = "cloud.smarthq.domain.light"
_SMOKER = "cloud.smarthq.device.smoker"
_LIGHT_DEVICE = "cloud.smarthq.device.light"


def _indexed_store(device_id: str = "device-1") -> dict:
    """One device whose only service tuple is the smoker light toggle."""
    return {
        device_id: {
            "snapshot": {
                "services": {
                    "svc-1": {
                        "on": False,
                        "serviceType": _TOGGLE,
                        "domainType": _LIGHT_DOMAIN,
                        "serviceDeviceType": _SMOKER,
                    }
                },
                "index": {(_TOGGLE, _LIGHT_DOMAIN, _SMOKER): "svc-1"},
                "raw": {},
            }
        }
    }


def _device_event(device_id: str, service_device_type: str, *, event: str = "updated") -> dict:
    """pubsub#device carrying one service tuple."""
    return {
        "kind": "pubsub#device",
        "deviceId": device_id,
        "event": event,
        "services": [
            {
                "serviceId": "svc-1",
                "serviceType": _TOGGLE,
                "domainType": _LIGHT_DOMAIN,
                "serviceDeviceType": service_device_type,
                "state": {"on": True},
            }
        ],
    }


def _reload_hass() -> MagicMock:
    """Hass whose async_create_task runs the reload coroutine."""
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()

    def _create_task(coro):
        return asyncio.get_running_loop().create_task(coro)

    hass.async_create_task.side_effect = _create_task
    return hass


async def test_device_item_index_keeps_distinct_service_device_types() -> None:
    """REST device snapshots index each (type, domain, serviceDeviceType)."""
    device_id = "device-1"
    store: dict = {device_id: {}}
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)
    websocket._write_device_item_to_store(
        device_id,
        {
            "services": [
                {
                    "serviceId": "svc-smoker",
                    "serviceType": _TOGGLE,
                    "domainType": _LIGHT_DOMAIN,
                    "serviceDeviceType": _SMOKER,
                    "state": {"on": False},
                },
                {
                    "serviceId": "svc-light",
                    "serviceType": _TOGGLE,
                    "domainType": _LIGHT_DOMAIN,
                    "serviceDeviceType": _LIGHT_DEVICE,
                    "state": {"on": True},
                },
            ]
        },
    )

    index = store[device_id]["snapshot"]["index"]
    assert index[(_TOGGLE, _LIGHT_DOMAIN, _SMOKER)] == "svc-smoker"
    assert index[(_TOGGLE, _LIGHT_DOMAIN, _LIGHT_DEVICE)] == "svc-light"
    assert len(index) == 2


async def test_metadata_lookup_reads_service_device_type_from_index() -> None:
    """Index lookups use the 3-tuple, including serviceDeviceType."""
    device_id = "device-abcdef12"
    service_id = "svc-1"
    store = {
        device_id: {
            "info": {},
            "snapshot": {
                "services": {},
                "index": {(_TOGGLE, _LIGHT_DOMAIN, _LIGHT_DEVICE): service_id},
                "raw": {},
            },
        }
    }
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)

    meta = websocket._get_service_metadata(device_id, service_id)

    assert meta["serviceType"] == _TOGGLE
    assert meta["domainType"] == _LIGHT_DOMAIN
    assert meta["serviceDeviceType"] == _LIGHT_DEVICE


async def test_service_update_indexes_three_tuples_and_does_not_reload() -> None:
    """pubsub#service updates state and the 3-tuple index, and does not reload."""
    device_id = "device-1"
    store = _indexed_store(device_id)
    hass = _reload_hass()
    websocket = SmartHQWebsocket(
        hass, api=MagicMock(), device_ids=[device_id], store=store, entry_id="entry-1"
    )
    payload = {
        "kind": "pubsub#service",
        "deviceId": device_id,
        "services": [
            {
                "serviceId": "svc-1",
                "serviceType": _TOGGLE,
                "domainType": _LIGHT_DOMAIN,
                "serviceDeviceType": _SMOKER,
                "state": {"on": True},
            },
            {
                "serviceId": "svc-2",
                "serviceType": _TOGGLE,
                "domainType": _LIGHT_DOMAIN,
                "serviceDeviceType": _LIGHT_DEVICE,
                "state": {"on": False},
            },
        ],
    }

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(payload)

    index = store[device_id]["snapshot"]["index"]
    assert index[(_TOGGLE, _LIGHT_DOMAIN, _SMOKER)] == "svc-1"
    assert index[(_TOGGLE, _LIGHT_DOMAIN, _LIGHT_DEVICE)] == "svc-2"
    assert store[device_id]["snapshot"]["services"]["svc-1"]["on"] is True
    assert websocket._reload_handle is None
    hass.config_entries.async_reload.assert_not_called()


async def test_device_event_with_same_tuples_does_not_reload() -> None:
    """A pubsub#device that repeats the current service tuples does not reload."""
    device_id = "device-1"
    store = _indexed_store(device_id)
    hass = _reload_hass()
    websocket = SmartHQWebsocket(
        hass, api=MagicMock(), device_ids=[device_id], store=store, entry_id="entry-1"
    )

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send") as mock_dispatch:
        await websocket._on_message(_device_event(device_id, _SMOKER))

    assert store[device_id]["snapshot"]["services"]["svc-1"]["on"] is False
    assert websocket._reload_handle is None
    hass.config_entries.async_reload.assert_not_called()
    mock_dispatch.assert_not_called()


async def test_device_tuple_change_schedules_reload() -> None:
    """A new serviceDeviceType on pubsub#device schedules one reload."""
    device_id = "device-1"
    store = _indexed_store(device_id)
    hass = _reload_hass()
    websocket = SmartHQWebsocket(
        hass, api=MagicMock(), device_ids=[device_id], store=store, entry_id="entry-1"
    )

    await websocket._on_message(_device_event(device_id, _LIGHT_DEVICE))

    assert websocket._reload_handle is not None
    websocket._cancel_entry_reload()
    hass.config_entries.async_reload.assert_not_called()


async def test_new_device_and_removed_device_schedule_reload() -> None:
    """Adding or removing a device id schedules a reload."""
    device_id = "device-1"
    store = _indexed_store(device_id)
    hass = _reload_hass()
    websocket = SmartHQWebsocket(
        hass, api=MagicMock(), device_ids=[device_id], store=store, entry_id="entry-1"
    )

    created = _device_event("device-2", _SMOKER, event="created")
    await websocket._on_message(created)
    assert websocket._reload_handle is not None
    websocket._cancel_entry_reload()

    await websocket._on_message(_device_event(device_id, _SMOKER, event="deleted"))
    assert websocket._reload_handle is not None
    websocket._cancel_entry_reload()
    hass.config_entries.async_reload.assert_not_called()


async def test_device_set_changes_debounce_to_one_reload() -> None:
    """Several pubsub#device changes schedule a single config-entry reload."""
    device_id = "device-1"
    store = _indexed_store(device_id)
    hass = _reload_hass()
    websocket = SmartHQWebsocket(
        hass, api=MagicMock(), device_ids=[device_id], store=store, entry_id="entry-1"
    )

    await websocket._on_message(_device_event("device-new-1", _SMOKER, event="created"))
    first = websocket._reload_handle
    assert first is not None
    await websocket._on_message(_device_event("device-new-2", _LIGHT_DEVICE, event="created"))
    assert websocket._reload_handle is not None
    assert websocket._reload_handle is not first
    assert first.cancelled()

    websocket._cancel_entry_reload()
    websocket._fire_entry_reload()
    await asyncio.sleep(0)

    hass.config_entries.async_reload.assert_awaited_once_with("entry-1")


def _service_tuple(service_device_type: str, service_id: str = "svc-1") -> dict:
    return {
        "serviceId": service_id,
        "serviceType": _TOGGLE,
        "domainType": _LIGHT_DOMAIN,
        "serviceDeviceType": service_device_type,
    }


async def test_wrapped_device_service_change_schedules_reload() -> None:
    """item/body wrappers use the same precedence as the device id for reload comparison."""
    device_id = "device-1"
    store = _indexed_store(device_id)
    hass = _reload_hass()
    websocket = SmartHQWebsocket(
        hass, api=MagicMock(), device_ids=[device_id], store=store, entry_id="entry-1"
    )

    await websocket._on_message(
        {
            "kind": "pubsub#device",
            "item": {
                "deviceId": device_id,
                "event": "updated",
                "services": [_service_tuple(_LIGHT_DEVICE)],
            },
        }
    )
    assert websocket._reload_handle is not None
    websocket._cancel_entry_reload()

    await websocket._on_message(
        {
            "kind": "pubsub#device",
            "body": {"deviceId": device_id, "event": "deleted"},
        }
    )
    assert websocket._reload_handle is not None
    websocket._cancel_entry_reload()

    await websocket._on_message(
        {
            "kind": "pubsub#device",
            "event": "updated",
            "services": [_service_tuple(_SMOKER)],
            "item": {
                "deviceId": device_id,
                "event": "deleted",
                "services": [_service_tuple(_LIGHT_DEVICE)],
            },
        }
    )
    assert websocket._reload_handle is None
    hass.config_entries.async_reload.assert_not_called()


async def test_device_event_reloads_after_service_event_inserts_tuple() -> None:
    """A service insert must not hide the new tuple from the next device event."""
    device_id = "device-1"
    store = _indexed_store(device_id)
    hass = _reload_hass()
    websocket = SmartHQWebsocket(
        hass, api=MagicMock(), device_ids=[device_id], store=store, entry_id="entry-1"
    )
    service_event = {
        "kind": "pubsub#service",
        "deviceId": device_id,
        "serviceId": "svc-2",
        "serviceType": _TOGGLE,
        "domainType": _LIGHT_DOMAIN,
        "serviceDeviceType": _LIGHT_DEVICE,
        "state": {"on": True},
    }
    device_event = {
        "kind": "pubsub#device",
        "deviceId": device_id,
        "event": "updated",
        "services": [_service_tuple(_SMOKER), _service_tuple(_LIGHT_DEVICE, "svc-2")],
    }

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(service_event)
    assert websocket._reload_handle is None
    assert (_TOGGLE, _LIGHT_DOMAIN, _LIGHT_DEVICE) in {
        (service.get("serviceType"), service.get("domainType"), service.get("serviceDeviceType"))
        for service in store[device_id]["snapshot"]["services"].values()
    }

    await websocket._on_message(device_event)

    assert websocket._reload_handle is not None
    websocket._cancel_entry_reload()
    hass.config_entries.async_reload.assert_not_called()


_LAUNDRY_STATE = "cloud.smarthq.service.laundry.state.v1"
_LAUNDRY_DOMAIN = "cloud.smarthq.domain.laundry"
_WASHER = "cloud.smarthq.device.washer"


def _laundry_store(device_id: str = "device-1") -> dict:
    """Washer whose laundry state index key includes serviceDeviceType."""
    return {
        device_id: {
            "snapshot": {
                "services": {
                    "laundry-1": {
                        "serviceType": _LAUNDRY_STATE,
                        "domainType": _LAUNDRY_DOMAIN,
                        "serviceDeviceType": _WASHER,
                        "runStatus": "cloud.smarthq.type.runstatus.delayed",
                    }
                },
                "index": {(_LAUNDRY_STATE, _LAUNDRY_DOMAIN, _WASHER): "laundry-1"},
                "raw": {},
            }
        }
    }


def _assert_laundry_index_keeps_cached_component(store: dict, device_id: str) -> None:
    index = store[device_id]["snapshot"]["index"]
    state = store[device_id]["snapshot"]["services"]["laundry-1"]
    assert index == {(_LAUNDRY_STATE, _LAUNDRY_DOMAIN, _WASHER): "laundry-1"}
    assert state["serviceDeviceType"] == _WASHER
    assert state["runStatus"] == "cloud.smarthq.type.runstatus.running"


async def test_service_list_omitting_component_reuses_cached_index_key() -> None:
    """A services-array update without serviceDeviceType must not add an empty index key."""
    device_id = "device-1"
    store = _laundry_store(device_id)
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(
            {
                "kind": "pubsub#service",
                "deviceId": device_id,
                "services": [
                    {
                        "serviceId": "laundry-1",
                        "serviceType": _LAUNDRY_STATE,
                        "domainType": _LAUNDRY_DOMAIN,
                        "state": {"runStatus": "cloud.smarthq.type.runstatus.running"},
                    }
                ],
            }
        )

    _assert_laundry_index_keeps_cached_component(store, device_id)


async def test_single_service_omitting_component_reuses_cached_index_key() -> None:
    """A single service update without serviceDeviceType must not add an empty index key."""
    device_id = "device-1"
    store = _laundry_store(device_id)
    websocket = SmartHQWebsocket(MagicMock(), api=MagicMock(), device_ids=[device_id], store=store)

    with patch("custom_components.smarthq.ws_client.async_dispatcher_send"):
        await websocket._on_message(
            {
                "kind": "pubsub#service",
                "deviceId": device_id,
                "serviceId": "laundry-1",
                "serviceType": _LAUNDRY_STATE,
                "domainType": _LAUNDRY_DOMAIN,
                "state": {"runStatus": "cloud.smarthq.type.runstatus.running"},
            }
        )

    _assert_laundry_index_keeps_cached_component(store, device_id)
