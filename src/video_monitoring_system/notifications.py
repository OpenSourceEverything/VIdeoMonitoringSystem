"""Owner-private Web Push enrollment and delivery for the recorder."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .provisioning import secure_write_text


_STATE_FILE = "push-notifications.json"
_MAX_SUBSCRIPTIONS = 8
_MAX_ENDPOINT_LENGTH = 2048


class PushNotificationError(ValueError):
    """A safe error message that can be displayed to the owner."""


def _decode_key(value: object, *, expected_size: int) -> str:
    if not isinstance(value, str) or len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PushNotificationError("The phone's notification details are invalid.")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as error:
        raise PushNotificationError("The phone's notification details are invalid.") from error
    if len(raw) != expected_size:
        raise PushNotificationError("The phone's notification details are invalid.")
    if expected_size == 65 and raw[0] != 4:
        raise PushNotificationError("The phone's notification details are invalid.")
    return value


def _validate_subscription(subscription: Mapping[str, object]) -> dict[str, object]:
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= _MAX_ENDPOINT_LENGTH:
        raise PushNotificationError("The phone's notification details are invalid.")
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError as error:
        raise PushNotificationError("The phone's notification details are invalid.") from error
    # First pilot targets Chrome on Android. An exact push-service host avoids
    # turning an owner-submitted endpoint into a server-side request forgery.
    if (
        parts.scheme != "https"
        or parts.hostname != "fcm.googleapis.com"
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/")
        or parts.path == "/"
        or parts.query
        or parts.fragment
    ):
        raise PushNotificationError("Use Chrome on Android for this first notification test.")
    if not isinstance(keys, Mapping):
        raise PushNotificationError("The phone's notification details are invalid.")
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": _decode_key(keys.get("p256dh"), expected_size=65),
            "auth": _decode_key(keys.get("auth"), expected_size=16),
        },
    }


def _subscription_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


class PushNotificationService:
    """Keep VAPID identity and phone subscriptions in recorder-private state."""

    def __init__(
        self,
        data_directory: Path,
        *,
        sender: Callable[..., object] = webpush,
    ) -> None:
        self.path = Path(data_directory) / _STATE_FILE
        self._sender = sender
        self._lock = threading.RLock()

    def _state(self) -> dict[str, object]:
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    state.get("version") == 1
                    and isinstance(state.get("private_key"), str)
                    and isinstance(state.get("subscriptions"), dict)
                ):
                    return state
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            raise PushNotificationError("Saved notification settings could not be read.")
        key = ec.generate_private_key(ec.SECP256R1())
        private_der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        state = {
            "version": 1,
            "private_key": base64.urlsafe_b64encode(private_der).decode("ascii").rstrip("="),
            "subscriptions": {},
        }
        self._save(state)
        return state

    def _save(self, state: Mapping[str, object]) -> None:
        secure_write_text(self.path, json.dumps(state, separators=(",", ":")) + "\n")

    def public_key(self) -> str:
        with self._lock:
            encoded_key = str(self._state()["private_key"])
            private_der = base64.urlsafe_b64decode(encoded_key + "=" * (-len(encoded_key) % 4))
            key = serialization.load_der_private_key(private_der, password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise PushNotificationError("Saved notification key is invalid.")
            public_bytes = key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            return base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")

    def subscribe(self, subscription: Mapping[str, object]) -> str:
        validated = _validate_subscription(subscription)
        endpoint = str(validated["endpoint"])
        identifier = _subscription_id(endpoint)
        with self._lock:
            state = self._state()
            subscriptions = state["subscriptions"]
            assert isinstance(subscriptions, dict)
            if identifier not in subscriptions and len(subscriptions) >= _MAX_SUBSCRIPTIONS:
                raise PushNotificationError("Too many phones are enrolled.")
            if subscriptions.get(identifier) == validated:
                return identifier
            subscriptions[identifier] = validated
            self._save(state)
        return identifier

    def unsubscribe(self, endpoint: str) -> None:
        if not isinstance(endpoint, str) or len(endpoint) > _MAX_ENDPOINT_LENGTH:
            raise PushNotificationError("This phone's subscription is invalid.")
        with self._lock:
            state = self._state()
            subscriptions = state["subscriptions"]
            assert isinstance(subscriptions, dict)
            if subscriptions.pop(_subscription_id(endpoint), None) is not None:
                self._save(state)

    def send_test(self, endpoint: str, *, contact_uri: str) -> None:
        if not isinstance(endpoint, str) or len(endpoint) > _MAX_ENDPOINT_LENGTH:
            raise PushNotificationError("Enable notifications on this phone first.")
        identifier = _subscription_id(endpoint)
        with self._lock:
            state = self._state()
            subscriptions = state["subscriptions"]
            assert isinstance(subscriptions, dict)
            subscription = subscriptions.get(identifier)
            if not isinstance(subscription, dict):
                raise PushNotificationError("Enable notifications on this phone first.")
            encoded_key = str(state["private_key"])
        if not contact_uri.startswith("https://"):
            raise PushNotificationError("Remote access must be ready before sending a test.")
        self._send(
            identifier,
            subscription,
            encoded_key=encoded_key,
            contact_uri=contact_uri,
            payload={
                "title": "OSE Cameras",
                "body": "Test notification",
                "url": "/events",
                "tag": "ose-cameras-test",
            },
            ttl=60,
        )

    def send_event(
        self,
        *,
        title: str,
        body: str,
        url: str,
        tag: str,
        contact_uri: str,
    ) -> int:
        """Send one minimal event notice to every enrolled owner phone."""

        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 80
            or not isinstance(body, str)
            or not 1 <= len(body) <= 160
            or not isinstance(url, str)
            or not url.startswith("/events?")
            or url.startswith("//")
            or len(url) > 512
            or not isinstance(tag, str)
            or not re.fullmatch(r"[a-z0-9-]{1,80}", tag)
        ):
            raise PushNotificationError("Event notification is invalid.")
        if not contact_uri.startswith("https://"):
            raise PushNotificationError("Remote access must be ready before sending alerts.")
        with self._lock:
            state = self._state()
            subscriptions = state["subscriptions"]
            assert isinstance(subscriptions, dict)
            selected = tuple(
                (identifier, dict(subscription))
                for identifier, subscription in subscriptions.items()
                if isinstance(identifier, str) and isinstance(subscription, dict)
            )
            encoded_key = str(state["private_key"])
        if not selected:
            raise PushNotificationError("No owner phone is enrolled for alerts.")
        delivered = 0
        last_error: PushNotificationError | None = None
        payload = {"title": title, "body": body, "url": url, "tag": tag}
        for identifier, subscription in selected:
            try:
                self._send(
                    identifier,
                    subscription,
                    encoded_key=encoded_key,
                    contact_uri=contact_uri,
                    payload=payload,
                    ttl=3600,
                )
            except PushNotificationError as error:
                last_error = error
            else:
                delivered += 1
        if delivered == 0:
            raise last_error or PushNotificationError(
                "The event alert could not reach an owner phone."
            )
        return delivered

    def _send(
        self,
        identifier: str,
        subscription: Mapping[str, object],
        *,
        encoded_key: str,
        contact_uri: str,
        payload: Mapping[str, str],
        ttl: int,
    ) -> None:
        if not contact_uri.startswith("https://"):
            raise PushNotificationError("Remote access must be ready before sending alerts.")
        try:
            self._sender(
                subscription_info=dict(subscription),
                data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=encoded_key,
                vapid_claims={"sub": contact_uri},
                # These are user-visible alerts, not background synchronization.
                # Without this header Web Push defaults to normal urgency, which
                # permits deferral while a battery-powered phone is dormant.
                headers={"Urgency": "high"},
                ttl=ttl,
                timeout=8,
            )
        except WebPushException as error:
            if getattr(error.response, "status_code", None) in (404, 410):
                with self._lock:
                    state = self._state()
                    saved = state["subscriptions"]
                    assert isinstance(saved, dict)
                    if saved.pop(identifier, None) is not None:
                        self._save(state)
                raise PushNotificationError(
                    "An owner phone subscription expired. Enable it again."
                ) from error
            raise PushNotificationError(
                "The phone's push service rejected the notification."
            ) from error
        except (OSError, ValueError) as error:
            raise PushNotificationError(
                "The notification could not reach the phone's push service."
            ) from error
