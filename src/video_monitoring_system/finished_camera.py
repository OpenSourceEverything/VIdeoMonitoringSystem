"""Qualification boundary for a finished, standards-based camera.

The owner flow never asks for an IP address or RTSP URL.  A model-specific
driver discovers and configures the camera through its reviewed local setup
surface, then returns this secret-free evidence.  Only an exact qualified
model/revision/firmware tuple may finish the common OSE setup transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .camera_setup import CameraSetupBinding, VerifiedCameraEnrollment


class FinishedCameraError(RuntimeError):
    """A finished camera did not satisfy its qualified contract."""


_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+:/()-]{0,127}")


@dataclass(frozen=True, slots=True)
class QualifiedFinishedCamera:
    """Exact hardware and firmware allowed by one supported-camera entry."""

    manufacturer: str
    model: str
    hardware_revision: str
    firmware_versions: tuple[str, ...]
    wifi_bands_ghz: tuple[float, ...]

    def __post_init__(self) -> None:
        for value in (
            self.manufacturer,
            self.model,
            self.hardware_revision,
            *self.firmware_versions,
        ):
            if not isinstance(value, str) or _TEXT.fullmatch(value) is None:
                raise ValueError("Finished camera profile is invalid.")
        if not self.firmware_versions or len(set(self.firmware_versions)) != len(
            self.firmware_versions
        ):
            raise ValueError("Finished camera profile is invalid.")
        if not self.wifi_bands_ghz or any(
            band not in {2.4, 5.0, 6.0} for band in self.wifi_bands_ghz
        ):
            raise ValueError("Finished camera profile is invalid.")


@dataclass(frozen=True, slots=True)
class FinishedCameraEvidence:
    """Secret-free evidence returned by a model-specific local driver."""

    manufacturer: str
    model: str
    hardware_revision: str
    firmware_version: str
    permanent_identity: str
    main_codec: str
    detection_codec: str
    main_stream_advancing: bool
    detection_stream_advancing: bool
    cloud_blocked: bool


@runtime_checkable
class FinishedCameraDriver(Protocol):
    """Discover, configure, and probe one physical camera locally."""

    def verify(self, binding: CameraSetupBinding) -> FinishedCameraEvidence: ...


class FinishedCameraSetupTransport:
    """Adapt exact finished-camera evidence to the common OSE setup state."""

    name = "finished-camera"

    def __init__(
        self,
        profile: QualifiedFinishedCamera,
        driver: FinishedCameraDriver,
    ) -> None:
        if not isinstance(profile, QualifiedFinishedCamera):
            raise TypeError("A qualified finished camera profile is required.")
        if not isinstance(driver, FinishedCameraDriver):
            raise TypeError("A finished camera driver is required.")
        self.profile = profile
        self.driver = driver

    def verified_enrollment(
        self, binding: CameraSetupBinding
    ) -> VerifiedCameraEnrollment | None:
        if not isinstance(binding, CameraSetupBinding):
            raise TypeError("A camera setup binding is required.")
        if binding.transport != self.name:
            raise FinishedCameraError("Camera setup selected the wrong transport.")
        evidence = self.driver.verify(binding)
        if not isinstance(evidence, FinishedCameraEvidence):
            raise FinishedCameraError("Finished camera evidence is invalid.")
        exact_hardware = (
            evidence.manufacturer,
            evidence.model,
            evidence.hardware_revision,
        ) == (
            self.profile.manufacturer,
            self.profile.model,
            self.profile.hardware_revision,
        )
        if (
            not exact_hardware
            or evidence.firmware_version not in self.profile.firmware_versions
            or evidence.permanent_identity != binding.permanent_identity
            or evidence.main_codec.lower() != "h264"
            or evidence.detection_codec.lower() != "h264"
            or evidence.main_stream_advancing is not True
            or evidence.detection_stream_advancing is not True
            or evidence.cloud_blocked is not True
        ):
            raise FinishedCameraError(
                "The camera did not match the qualified local video contract."
            )
        return VerifiedCameraEnrollment(
            transaction_id=binding.transaction_id,
            camera_id=binding.camera_id,
            recorder_id=binding.recorder_id,
            bootstrap_identity=binding.bootstrap_identity,
            permanent_identity=binding.permanent_identity,
            transport=binding.transport,
        )


__all__ = [
    "FinishedCameraDriver",
    "FinishedCameraError",
    "FinishedCameraEvidence",
    "FinishedCameraSetupTransport",
    "QualifiedFinishedCamera",
]
