"""Device administration inputs (W7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.devices import DeviceCreate, DeviceRead, DeviceUpdate, DeviceWithToken


def test_an_unknown_timezone_is_refused() -> None:
    with pytest.raises(ValidationError):
        DeviceCreate(device_id="D1", name="Gate", assumed_timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        DeviceUpdate(assumed_timezone="Nowhere")
    assert DeviceCreate(device_id="D1", name="Gate").assumed_timezone == "Asia/Kolkata"


def test_the_silence_threshold_is_bounded() -> None:
    with pytest.raises(ValidationError):
        DeviceCreate(device_id="D1", name="Gate", silence_threshold_minutes=1)


def test_only_the_token_response_carries_a_token() -> None:
    assert "push_token" not in DeviceRead.model_fields
    assert "push_token" in DeviceWithToken.model_fields
    assert "push_token_hash" not in DeviceRead.model_fields
