"""Factory claim-label generation."""

from __future__ import annotations

import json
import os
import re
from html import escape
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .model import SOFTWARE_CAMERA_LIMIT
from .provisioning import DeviceIdentity, secure_write_text


class ClaimPayloadError(ValueError):
    """A scanned camera label is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class CameraClaim:
    kit_id: str
    device_id: str
    camera_number: int
    claim_code: str


_KIT_ID = re.compile(r"KIT-[A-F0-9]{12}")
_CAMERA_ID = re.compile(r"CAM-[A-F0-9]{16}")
_CLAIM_CODE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_LOCAL_CLAIM_ORIGIN = "http://osecameras.local/"


def camera_code(camera: DeviceIdentity) -> str:
    """Return the one-time claim secret formatted for careful manual entry."""

    if camera.role != "camera" or not camera.claim_secret:
        raise ValueError("a camera with an unused claim secret is required")
    return " ".join(
        camera.claim_secret[index : index + 4]
        for index in range(0, len(camera.claim_secret), 4)
    )


def parse_camera_code(value: str) -> str:
    """Normalize a printed camera code without weakening its case-sensitive value."""

    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ClaimPayloadError("Camera code is invalid.")
    normalized = re.sub(r"[ \t\r\n]", "", value)
    if _CLAIM_CODE.fullmatch(normalized) is None:
        raise ClaimPayloadError("Camera code is invalid.")
    return normalized


def claim_payload(camera: DeviceIdentity) -> str:
    if camera.role != "camera" or not camera.claim_secret:
        raise ValueError("a camera with an unused claim secret is required")
    # The private value remains in the fragment: browsers do not send it to the
    # recorder in the HTTP request, access log, or Referer header.  The local
    # page consumes it and immediately removes the fragment from history.
    return _LOCAL_CLAIM_ORIGIN + "#add-camera?" + urlencode(
        {
            "kit": camera.kit_id,
            "device": camera.device_id,
            "number": str(camera.camera_number),
            "code": camera.claim_secret,
        }
    )


def parse_claim_payload(value: str) -> CameraClaim:
    """Parse current browser labels and legacy ``vms://`` labels strictly."""

    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ClaimPayloadError("Camera QR code is invalid.")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "vms"
            and parsed.netloc == "claim"
            and parsed.path == ""
            and parsed.fragment == ""
        ):
            query = parsed.query
        elif (
            parsed.scheme in {"http", "https"}
            and parsed.hostname == "osecameras.local"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 80, 443}
            and parsed.path in {"", "/"}
            and parsed.query == ""
            and parsed.fragment.startswith("add-camera?")
        ):
            query = parsed.fragment.removeprefix("add-camera?")
        else:
            raise ValueError
        fields = parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
        if set(fields) != {"kit", "device", "number", "code"} or any(
            len(values) != 1 for values in fields.values()
        ):
            raise ValueError
        kit_id = fields["kit"][0]
        device_id = fields["device"][0]
        raw_number = fields["number"][0]
        claim_code = fields["code"][0]
        if (
            _KIT_ID.fullmatch(kit_id) is None
            or _CAMERA_ID.fullmatch(device_id) is None
            or not raw_number.isascii()
            or not raw_number.isdecimal()
            or not 1 <= int(raw_number) <= SOFTWARE_CAMERA_LIMIT
            or _CLAIM_CODE.fullmatch(claim_code) is None
        ):
            raise ValueError
    except (ValueError, UnicodeError) as error:
        raise ClaimPayloadError("Camera QR code is invalid.") from error
    return CameraClaim(
        kit_id=kit_id,
        device_id=device_id,
        camera_number=int(raw_number),
        claim_code=claim_code,
    )


def write_claim_label(camera: DeviceIdentity, output_directory: Path) -> Path:
    """Write a printable SVG label without exposing a private identity key."""

    import segno

    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(output_directory, 0o700)
    except OSError:
        pass
    path = output_directory / f"{camera.device_id}.svg"
    qr = segno.make(claim_payload(camera), error="m")
    qr.save(
        path,
        kind="svg",
        scale=6,
        border=3,
        title=f"Camera {camera.camera_number}: {camera.device_id}",
        desc="One-time Video Monitoring System claim label",
    )
    # Keep scanning primary while providing an equally valid no-camera-app
    # fallback. New codes are 32 hexadecimal characters, laid out as two
    # readable lines; legacy URL-safe codes remain printable and accepted.
    groups = camera_code(camera).split(" ")
    lines = [" ".join(groups[index : index + 4]) for index in range(0, len(groups), 4)]
    svg = path.read_text(encoding="utf-8")
    extra_height = 42 + (len(lines) * 18)
    svg = svg.replace(
        'width="330" height="330"',
        f'width="330" height="{330 + extra_height}"',
        1,
    )
    text = [
        '<text x="165" y="350" text-anchor="middle" '
        'font-family="sans-serif" font-size="13" font-weight="bold">'
        "Camera code</text>"
    ]
    for index, line in enumerate(lines):
        text.append(
            f'<text x="165" y="{370 + index * 18}" text-anchor="middle" '
            'font-family="monospace" font-size="13">'
            f"{escape(line)}</text>"
        )
    svg = svg.replace("</svg>", "".join(text) + "</svg>", 1)
    path.write_text(svg, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def write_label_index(cameras: tuple[DeviceIdentity, ...], output_directory: Path) -> Path:
    """Write a private print index containing one-time claim payloads."""

    content = {
        "schema_version": 1,
        "cameras": [
            {
                "camera_number": camera.camera_number,
                "device_id": camera.device_id,
                "camera_code": camera_code(camera),
                "payload": claim_payload(camera),
            }
            for camera in cameras
        ],
    }
    path = Path(output_directory) / "claim-labels.json"
    secure_write_text(path, json.dumps(content, indent=2, sort_keys=True) + "\n")
    return path


__all__ = [
    "CameraClaim",
    "ClaimPayloadError",
    "camera_code",
    "claim_payload",
    "parse_camera_code",
    "parse_claim_payload",
    "write_claim_label",
    "write_label_index",
]
