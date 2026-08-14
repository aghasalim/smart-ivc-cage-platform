from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator


def _utc_iso(dt: datetime) -> str:
    """Ensure datetime serializes with explicit UTC timezone (so JS parses it correctly)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


UtcDt = Annotated[datetime, PlainSerializer(_utc_iso, return_type=str, when_used="json")]


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class LoginIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=4, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: str


class CageIn(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    location: str = ""
    animal_strain: str = ""
    animal_age_days: int = 0


class CagePatch(BaseModel):
    name: str | None = None
    location: str | None = None
    animal_strain: str | None = None
    animal_age_days: int | None = None


class CageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    location: str
    animal_strain: str
    animal_age_days: int
    last_seen: UtcDt | None
    created_at: UtcDt


SensorName = Literal["feeding", "water", "metabolic", "behaviour", "weighing", "environment"]


class FeedingPayload(BaseModel):
    delivered_g: float = Field(ge=0)
    remaining_g: float = Field(ge=0)
    wasted_g: float = Field(ge=0)
    gate_state: Literal["open", "closed"]


class WaterPayload(BaseModel):
    flow_ml: float = Field(ge=0)
    tank_ml: float = Field(ge=0)


class MetabolicPayload(BaseModel):
    vo2_ml_min_kg: float = Field(ge=0)
    vco2_ml_min_kg: float = Field(ge=0)
    rer: float = Field(ge=0, le=2)
    ee_kcal_h: float = Field(ge=0)


class BehaviourPayload(BaseModel):
    grid_cells: list[int]
    movement_cm: float = Field(ge=0)

    @field_validator("grid_cells")
    @classmethod
    def _grid_len(cls, v: list[int]) -> list[int]:
        if len(v) != 16:
            raise ValueError("grid_cells must contain 16 values (4x4 grid)")
        return v


class WeighingPayload(BaseModel):
    animal_g: float = Field(ge=0)


class EnvironmentPayload(BaseModel):
    """Ambient cage environment — all fields optional so partial readings
    (e.g. CO2+O2 from Arduino before a DHT is wired) are accepted."""
    temperature_c: float | None = Field(default=None, ge=-40, le=85)
    humidity_pct:  float | None = Field(default=None, ge=0,   le=100)
    co2_ppm:       int   | None = Field(default=None, ge=0,   le=10000)
    o2_pct:        float | None = Field(default=None, ge=0,   le=25)


class IngestSensors(BaseModel):
    feeding: FeedingPayload | None = None
    water: WaterPayload | None = None
    metabolic: MetabolicPayload | None = None
    behaviour: BehaviourPayload | None = None
    weighing: WeighingPayload | None = None
    environment: EnvironmentPayload | None = None


class IngestIn(BaseModel):
    cage_id: str = Field(min_length=1, max_length=64)
    ts: datetime
    seq: int = Field(ge=0)
    sensors: IngestSensors


class IngestOut(BaseModel):
    received: bool
    stored: int


class ReadingOut(BaseModel):
    ts: UtcDt
    sensor: SensorName
    payload: dict[str, Any]


class SummaryDay(BaseModel):
    date: str
    feeding: dict[str, float]
    water_ml: float
    metabolic: dict[str, float]
    behaviour: dict[str, int]


class SummaryOut(BaseModel):
    cage_id: str
    days: list[SummaryDay]


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cage_id: str
    ts: UtcDt
    severity: str
    rule: str
    message: str
    acknowledged_at: UtcDt | None


class ScheduleWindowIn(BaseModel):
    start_local: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_local: str = Field(pattern=r"^\d{2}:\d{2}$")
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    active: bool = True
    # IANA timezone (e.g. "Europe/Brussels", "America/New_York"). Defaults to
    # Belgium because the lab lives there. The backend stores HH:MM verbatim
    # and the scheduler / UI interpret it in this timezone.
    timezone: str = Field(default="Europe/Brussels", max_length=64)


class ScheduleOut(BaseModel):
    cage_id: str
    windows: list[ScheduleWindowIn]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
