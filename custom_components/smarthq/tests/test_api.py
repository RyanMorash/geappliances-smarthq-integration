"""Tests for SmartHQ REST command outcomes."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.smarthq.api import COMMAND_OUTCOME_SUCCESS, SmartHQApi, SmartHQError

pytestmark = pytest.mark.asyncio


def _api() -> SmartHQApi:
    api = SmartHQApi(MagicMock(), MagicMock())
    api._request_json = AsyncMock()
    return api


def _command_kwargs() -> dict:
    return {
        "device_id": "device-1",
        "service_type": "cloud.smarthq.service.toggle",
        "domain_type": "cloud.smarthq.domain.light",
        "service_device_type": "cloud.smarthq.device.light",
        "command": {"commandType": "cloud.smarthq.command.toggle.set", "on": True},
    }


async def test_send_command_success_keeps_correlation_id() -> None:
    """A success outcome returns the body, including correlationId."""
    api = _api()
    body = {
        "kind": "service#command",
        "success": True,
        "correlationId": "7679c99a-cf49-457e-9668-dc83254afd24",
        "outcome": COMMAND_OUTCOME_SUCCESS,
        "services": [],
    }
    api._request_json.return_value = body

    result = await api.async_send_command(**_command_kwargs())

    assert result is body
    assert result["correlationId"] == "7679c99a-cf49-457e-9668-dc83254afd24"
    assert result["outcome"] == COMMAND_OUTCOME_SUCCESS


async def test_send_command_without_outcome_returns_correlation_id() -> None:
    """Missing outcome is still an acknowledgement and keeps correlationId."""
    api = _api()
    body = {"kind": "service#command", "success": True, "correlationId": "corr-2"}
    api._request_json.return_value = body

    result = await api.async_send_command(**_command_kwargs())

    assert result["correlationId"] == "corr-2"


@pytest.mark.parametrize(
    "outcome",
    [
        "cloud.smarthq.outcome.failed",
        "cloud.smarthq.outcome.timeout",
        "cloud.smarthq.outcome.unknown",
        "cloud.smarthq.outcome.servicedisabled",
    ],
)
async def test_send_command_rejects_non_success_outcome(outcome: str) -> None:
    """A present outcome other than success raises and names the correlation id."""
    api = _api()
    api._request_json.return_value = {
        "success": True,
        "correlationId": "corr-fail",
        "outcome": outcome,
    }

    with pytest.raises(SmartHQError, match="corr-fail"):
        await api.async_send_command(**_command_kwargs())


async def test_send_command_http_error_propagates() -> None:
    """HTTP errors from the command POST still raise SmartHQError."""
    api = _api()
    api._request_json.side_effect = SmartHQError("POST /v2/command -> 400: bad")

    with pytest.raises(SmartHQError, match="400"):
        await api.async_send_command(**_command_kwargs())
