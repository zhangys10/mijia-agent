from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from mijia_agent.app import create_app
from mijia_agent.climate_profile import ClimateProfileRequest, derive_climate_profile
from mijia_agent.config import Settings


def settings():
    return Settings(
        internal_secret="internal-secret-" * 3,
        tools_secret="tools-secret-" * 3,
        gateway_key="fake-key",
        gateway_url="https://gateway.example/v1",
        console_url="https://console.example",
        model="test-model",
        allowed_models=("test-model",),
    )


def observation(index: int, *, mode: str = "cool", excluded: bool = False):
    captured = datetime(2026, 1, 1, 8, tzinfo=timezone.utc) + timedelta(days=index)
    return {
        "observationRef": f"obs_{index:024x}",
        "roomRef": "room_" + "a" * 24,
        "actuatorRef": "actuator_" + "b" * 24,
        "ambientTemperature": 25.0,
        "targetTemperature": 23.0 + index % 2,
        "mode": mode,
        "powerOn": True,
        "capturedAt": captured.isoformat(),
        "localHour": 8,
        "dayOfWeek": captured.isoweekday(),
        "source": "external_override",
        "excluded": excluded,
        "previous": {"targetTemperature": 25.0, "mode": "cool"},
    }


def test_derives_learned_cell_without_model_access():
    request = ClimateProfileRequest.model_validate(
        {"timezone": "Asia/Shanghai", "observations": [observation(i) for i in range(6)]}
    )
    result = derive_climate_profile(request, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    learned = [cell for cell in result.rooms[0].cells if cell.status == "learned"]
    assert len(learned) == 1
    assert learned[0].timeWindow == "06-12"
    assert learned[0].ambientBand == "24-28"
    assert learned[0].derivedMode == "cool"
    assert learned[0].supportCount == 6


def test_sparse_excluded_and_conflicting_modes_stay_unlearned_or_mode_less():
    values = [observation(i, mode="cool" if i < 3 else "heat") for i in range(6)]
    values.append(observation(9, excluded=True))
    result = derive_climate_profile(ClimateProfileRequest.model_validate({"observations": values}))
    cell = next(cell for cell in result.rooms[0].cells if cell.supportCount)
    assert cell.status == "learned"
    assert cell.derivedMode is None
    assert cell.supportCount == 6


def test_shared_room_observations_are_separated_by_controller():
    boiler = [observation(i, mode="heat") for i in range(6)]
    thermostat = [
        {**observation(i + 10, mode="auto"), "actuatorRef": "actuator_" + "c" * 24}
        for i in range(6)
    ]
    result = derive_climate_profile(
        ClimateProfileRequest.model_validate({"observations": boiler + thermostat})
    )
    assert len(result.rooms) == 2
    learned = {
        room.actuatorRef: next(cell for cell in room.cells if cell.status == "learned")
        for room in result.rooms
    }
    assert learned["actuator_" + "b" * 24].derivedMode == "heat"
    assert learned["actuator_" + "c" * 24].derivedMode == "auto"


def test_setpoint_only_controller_learns_temperature_without_inventing_mode():
    values = [
        {
            **observation(i),
            "mode": None,
            "previous": {"targetTemperature": 25.0, "mode": None},
        }
        for i in range(6)
    ]
    result = derive_climate_profile(
        ClimateProfileRequest.model_validate({"observations": values})
    )
    cell = next(cell for cell in result.rooms[0].cells if cell.status == "learned")
    assert cell.derivedTargetTemperature is not None
    assert cell.derivedMode is None


def test_http_contract_rejects_identifiers_and_authenticates():
    config = settings()
    client = TestClient(create_app(config))
    assert client.post("/internal/v1/climate-profile/derive", json={"observations": []}).status_code == 401
    headers = {"Authorization": f"Bearer {config.internal_secret}"}
    assert client.post(
        "/internal/v1/climate-profile/derive",
        headers=headers,
        json={"observations": [], "homeId": "must-not-enter-python"},
    ).status_code == 400
    response = client.post(
        "/internal/v1/climate-profile/derive", headers=headers, json={"observations": []}
    )
    assert response.status_code == 200
    assert response.json()["rooms"] == []
