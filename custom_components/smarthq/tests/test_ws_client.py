"""Tests for SmartHQ WebSocket state handling."""

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
