"""Strict, secret-free provisioning configuration.

Provisioning configuration files contain only ordinary product choices.  A
caller must obtain every credential through a private prompt after this file
has been validated.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from .regulatory import validate_wifi_country
from .model import SOFTWARE_CAMERA_LIMIT


CameraModel = Literal["zero-w", "zero-2-w"]
ConfigurationFormat = Literal["json", "yaml"]
ProvisioningMode = Literal["kit", "camera"]

_SCHEMA_VERSION = 1
_MAX_CONFIGURATION_BYTES = 64 * 1024
_CAMERA_MODELS = frozenset(("zero-w", "zero-2-w"))
_PROVISIONING_MODES = frozenset(("kit", "camera"))
_TOP_LEVEL_FIELDS = frozenset(
    (
        "schema_version",
        "mode",
        "camera_count",
        "camera_model",
        "camera_number",
        "network",
    )
)
_NETWORK_FIELDS = frozenset(("ssid", "credential_source", "country"))
_SECRET_FIELD_MARKERS = (
    "secret",
    "password",
    "passphrase",
    "token",
    "privatekey",
)


class ConfigurationError(ValueError):
    """A provisioning configuration is unsafe or invalid.

    Messages deliberately describe structure only.  They never contain a
    configuration value.
    """


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Non-secret network choices for provisioning."""

    credential_source: Literal["prompt"]
    ssid: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class ProvisioningConfig:
    """Validated product choices consumed by the provisioning application."""

    schema_version: Literal[1]
    mode: ProvisioningMode
    camera_model: CameraModel
    network: NetworkConfig
    camera_count: int | None = None
    camera_number: int | None = None


def build_provisioning_config(
    *,
    mode: ProvisioningMode,
    camera_model: CameraModel,
    camera_count: int | None = None,
    camera_number: int | None = None,
    ssid: str | None = None,
    country: str | None = None,
) -> ProvisioningConfig:
    """Validate choices collected by a CLI or GUI as the file schema."""

    raw: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "mode": mode,
        "camera_model": camera_model,
        "network": {
            "credential_source": "prompt",
            **({"ssid": ssid} if ssid is not None else {}),
            **({"country": country} if country is not None else {}),
        },
    }
    if camera_count is not None:
        raw["camera_count"] = camera_count
    if camera_number is not None:
        raw["camera_number"] = camera_number
    return _validate(raw)


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also refuses duplicate mapping keys."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None, None, "expected a mapping", node.start_mark
            )

        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an invalid mapping key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found a duplicate mapping key",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_provisioning_config(path: str | Path) -> ProvisioningConfig:
    """Read and validate one UTF-8 JSON or YAML provisioning file.

    The suffix must be ``.json``, ``.yaml``, or ``.yml`` so parsing does not
    depend on content guessing.
    """

    source = Path(path).expanduser()
    configuration_format = _format_for_path(source)
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise ConfigurationError("Provisioning configuration could not be read.") from error

    if len(payload) > _MAX_CONFIGURATION_BYTES:
        raise ConfigurationError("Provisioning configuration is too large.")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise ConfigurationError("Provisioning configuration must be UTF-8.") from error
    return parse_provisioning_config(text, format=configuration_format)


def parse_provisioning_config(
    text: str, *, format: ConfigurationFormat
) -> ProvisioningConfig:
    """Parse and validate JSON or YAML text without retaining secret values."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if len(text.encode("utf-8")) > _MAX_CONFIGURATION_BYTES:
        raise ConfigurationError("Provisioning configuration is too large.")

    if format == "json":
        raw = _parse_json(text)
    elif format == "yaml":
        raw = _parse_yaml(text)
    else:
        raise ConfigurationError("Provisioning configuration format is unsupported.")

    # Scan before reporting ordinary schema errors.  A file that contains a
    # secret is always rejected as secret-bearing, even if it is malformed in
    # some other way.  The value is never included in an exception.
    _reject_secret_fields(raw)
    return _validate(raw)


def _format_for_path(path: Path) -> ConfigurationFormat:
    suffix = path.suffix.casefold()
    if suffix == ".json":
        return "json"
    if suffix in (".yaml", ".yml"):
        return "yaml"
    raise ConfigurationError("Provisioning configuration format is unsupported.")


def _parse_json(text: str) -> Any:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigurationError(
                    "Provisioning configuration contains a duplicate field."
                )
            result[key] = value
        return result

    def reject_nonfinite(_: str) -> Any:
        raise ConfigurationError("Provisioning configuration contains an invalid number.")

    try:
        return json.loads(
            text,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_nonfinite,
        )
    except ConfigurationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ConfigurationError("Provisioning configuration is invalid JSON.") from error


def _parse_yaml(text: str) -> Any:
    try:
        # Aliases and anchors add no value to this tiny user-facing format and
        # can amplify input dramatically.  Refuse both before construction.
        for event in yaml.parse(text, Loader=_StrictSafeLoader):
            if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
                raise ConfigurationError(
                    "Provisioning configuration cannot use YAML aliases or anchors."
                )
        return yaml.load(text, Loader=_StrictSafeLoader)
    except ConfigurationError:
        raise
    except yaml.YAMLError as error:
        # SafeLoader rejects custom object tags as well as malformed YAML.
        raise ConfigurationError("Provisioning configuration is invalid YAML.") from error


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                normalized = "".join(character for character in key.casefold() if character.isalnum())
                if any(marker in normalized for marker in _SECRET_FIELD_MARKERS):
                    raise ConfigurationError(
                        "Provisioning configuration cannot contain secret fields."
                    )
            _reject_secret_fields(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            _reject_secret_fields(child)


def _validate(raw: Any) -> ProvisioningConfig:
    if not isinstance(raw, dict):
        raise ConfigurationError("Provisioning configuration must be an object.")
    if not all(isinstance(key, str) for key in raw):
        raise ConfigurationError("Provisioning configuration field names must be text.")

    unknown = set(raw) - _TOP_LEVEL_FIELDS
    missing = {"schema_version", "mode", "camera_model"} - set(raw)
    if unknown:
        raise ConfigurationError("Provisioning configuration contains unknown fields.")
    if missing:
        raise ConfigurationError("Provisioning configuration is missing required fields.")

    schema_version = raw["schema_version"]
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise ConfigurationError(
            "Provisioning configuration has an unsupported schema version."
        )
    mode = raw["mode"]
    if not isinstance(mode, str) or mode not in _PROVISIONING_MODES:
        raise ConfigurationError("Provisioning configuration has an invalid mode.")

    camera_model = raw["camera_model"]
    if not isinstance(camera_model, str) or camera_model not in _CAMERA_MODELS:
        raise ConfigurationError("Provisioning configuration has an invalid camera model.")

    camera_count_value = raw.get("camera_count")
    camera_count = None
    if camera_count_value is not None:
        camera_count = _camera_number(camera_count_value, "camera count")
    camera_number_value = raw.get("camera_number")
    camera_number = None
    if camera_number_value is not None:
        camera_number = _camera_number(camera_number_value, "camera number")

    if mode == "kit" and (camera_count is None or camera_number is not None):
        raise ConfigurationError(
            "Kit provisioning requires camera_count and forbids camera_number."
        )
    if mode == "camera" and (camera_number is None or camera_count is not None):
        raise ConfigurationError(
            "Camera provisioning requires camera_number and forbids camera_count."
        )

    network = _validate_network(
        raw.get("network", {"credential_source": "prompt"})
    )
    if mode == "kit" and network.ssid is not None:
        raise ConfigurationError("Kit provisioning cannot select a camera network name.")
    return ProvisioningConfig(
        schema_version=_SCHEMA_VERSION,
        mode=mode,
        camera_model=camera_model,
        network=network,
        camera_count=camera_count,
        camera_number=camera_number,
    )


def _camera_number(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= SOFTWARE_CAMERA_LIMIT
    ):
        raise ConfigurationError(f"Provisioning configuration has an invalid {label}.")
    return value


def _validate_network(raw: Any) -> NetworkConfig:
    if not isinstance(raw, dict):
        raise ConfigurationError("Provisioning network configuration must be an object.")
    if not all(isinstance(key, str) for key in raw):
        raise ConfigurationError("Provisioning network field names must be text.")
    if set(raw) - _NETWORK_FIELDS:
        raise ConfigurationError("Provisioning network contains unknown fields.")
    if "credential_source" not in raw:
        raise ConfigurationError("Provisioning network is missing credential_source.")
    if raw["credential_source"] != "prompt":
        raise ConfigurationError(
            "Provisioning network credential_source must be 'prompt'."
        )

    ssid = raw.get("ssid")
    if ssid is not None:
        if not isinstance(ssid, str) or not ssid or len(ssid.encode("utf-8")) > 32:
            raise ConfigurationError("Provisioning network has an invalid SSID.")
    country = raw.get("country")
    if country is not None:
        try:
            country = validate_wifi_country(country)
        except ValueError as error:
            raise ConfigurationError(
                "Provisioning network has an invalid Wi-Fi country code."
            ) from error
    return NetworkConfig(
        credential_source="prompt",
        ssid=ssid,
        country=country,
    )


__all__ = [
    "CameraModel",
    "ConfigurationError",
    "NetworkConfig",
    "ProvisioningConfig",
    "ProvisioningMode",
    "build_provisioning_config",
    "load_provisioning_config",
    "parse_provisioning_config",
]
