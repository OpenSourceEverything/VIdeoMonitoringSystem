"""Stable product-domain objects."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


Metadata = Mapping[str, object]

# Product hardware must qualify at eight cameras.  The software identity,
# enrollment, and configuration envelope deliberately leaves room for sixteen
# so a recorder upgrade does not require another state-format migration.
SOFTWARE_CAMERA_LIMIT = 16


@dataclass(frozen=True, slots=True, kw_only=True)
class Recorder:
    id: str
    name: str
    camera_limit: int = SOFTWARE_CAMERA_LIMIT
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class Camera:
    id: str
    number: int
    name: str | None = None
    state: str = "pending"
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class Recording:
    id: str
    camera_id: str
    started_at: datetime
    ended_at: datetime | None = None
    storage_reference: str | None = None
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class DetectionEvent:
    id: str
    camera_id: str
    recording_id: str
    occurred_at: datetime
    kind: str
    confidence: float | None = None
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class Alert:
    id: str
    detection_event_id: str
    created_at: datetime
    channel: str
    delivered_at: datetime | None = None
    metadata: Metadata = field(default_factory=dict)
