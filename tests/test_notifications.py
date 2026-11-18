import base64
import json
import tempfile
import unittest
from pathlib import Path

from py_vapid import Vapid
from pywebpush import WebPushException

from video_monitoring_system.notifications import (
    PushNotificationError,
    PushNotificationService,
)


def subscription(endpoint="https://fcm.googleapis.com/fcm/send/example"):
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": base64.urlsafe_b64encode(b"\x04" + b"a" * 64).decode().rstrip("="),
            "auth": base64.urlsafe_b64encode(b"b" * 16).decode().rstrip("="),
        },
    }


class PushNotificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.calls = []

        def sender(**values):
            self.calls.append(values)

        self.service = PushNotificationService(Path(self.directory.name), sender=sender)

    def test_key_and_enrollment_are_private_and_survive_restart(self):
        public_key = self.service.public_key()
        self.service.subscribe(subscription())
        state_path = Path(self.directory.name) / "push-notifications.json"
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(PushNotificationService(Path(self.directory.name)).public_key(), public_key)
        state = json.loads(state_path.read_text())
        self.assertEqual(len(state["subscriptions"]), 1)
        self.assertIsNotNone(Vapid.from_string(state["private_key"]).public_key)
        self.assertNotIn(subscription()["endpoint"], public_key)

    def test_test_delivery_targets_only_enrolled_phone_and_can_be_revoked(self):
        phone = subscription()
        self.service.subscribe(phone)
        with self.assertRaises(PushNotificationError):
            self.service.send_test("https://fcm.googleapis.com/fcm/send/other", contact_uri="https://osecameras.example.ts.net")
        self.service.send_test(phone["endpoint"], contact_uri="https://osecameras.example.ts.net")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["subscription_info"], phone)
        self.assertEqual(self.calls[0]["timeout"], 8)
        self.assertEqual(self.calls[0]["headers"], {"Urgency": "high"})
        self.assertEqual(self.calls[0]["ttl"], 60)
        self.assertEqual(json.loads(self.calls[0]["data"])["url"], "/events")
        self.service.unsubscribe(phone["endpoint"])
        with self.assertRaises(PushNotificationError):
            self.service.send_test(phone["endpoint"], contact_uri="https://osecameras.example.ts.net")

    def test_reenrollment_repairs_client_without_rewriting_unchanged_state(self):
        phone = subscription()
        identifier = self.service.subscribe(phone)

        def unexpected_save(state):
            self.fail("unchanged enrollment should not rewrite private state")

        self.service._save = unexpected_save
        self.assertEqual(self.service.subscribe(phone), identifier)

    def test_rejects_non_chrome_endpoint_and_invalid_keys(self):
        for endpoint in (
            "http://fcm.googleapis.com/fcm/send/example",
            "https://127.0.0.1/fcm/send/example",
            "https://fcm.googleapis.com.evil.example/fcm/send/example",
            "https://fcm.googleapis.com:444/fcm/send/example",
            "https://user@fcm.googleapis.com/fcm/send/example",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(PushNotificationError):
                self.service.subscribe(subscription(endpoint))
        invalid = subscription()
        invalid["keys"]["auth"] = "!not-base64!"
        with self.assertRaises(PushNotificationError):
            self.service.subscribe(invalid)

    def test_prevents_contactless_send_and_corrupt_state_regeneration(self):
        phone = subscription()
        self.service.subscribe(phone)
        with self.assertRaises(PushNotificationError):
            self.service.send_test(phone["endpoint"], contact_uri="")
        self.assertFalse(self.calls)
        state_path = Path(self.directory.name) / "push-notifications.json"
        state_path.write_text("not-json")
        with self.assertRaises(PushNotificationError):
            self.service.public_key()

    def test_expired_subscription_is_removed(self):
        phone = subscription()
        self.service.subscribe(phone)

        class Gone:
            status_code = 410

        def expired(**values):
            raise WebPushException("gone", response=Gone())

        self.service._sender = expired
        with self.assertRaisesRegex(PushNotificationError, "expired"):
            self.service.send_test(phone["endpoint"], contact_uri="https://osecameras.example.ts.net")
        state = json.loads(self.service.path.read_text())
        self.assertEqual(state["subscriptions"], {})

    def test_event_delivery_uses_exact_deep_link_and_survives_one_stale_phone(self):
        stale = subscription("https://fcm.googleapis.com/fcm/send/stale")
        current = subscription("https://fcm.googleapis.com/fcm/send/current")
        self.service.subscribe(stale)
        self.service.subscribe(current)

        class Gone:
            status_code = 410

        def sender(**values):
            if values["subscription_info"]["endpoint"] == stale["endpoint"]:
                raise WebPushException("gone", response=Gone())
            self.calls.append(values)

        self.service._sender = sender
        delivered = self.service.send_event(
            title="Person at Front Door",
            body="Tap to view the recorded event.",
            url="/events?event=frigate%3A1720000000.25-abcd12",
            tag="ose-event-abcd12",
            contact_uri="https://osecameras.example.ts.net",
        )

        self.assertEqual(delivered, 1)
        self.assertEqual(len(self.calls), 1)
        payload = json.loads(self.calls[0]["data"])
        self.assertEqual(
            payload["url"],
            "/events?event=frigate%3A1720000000.25-abcd12",
        )
        self.assertEqual(payload["title"], "Person at Front Door")
        self.assertEqual(self.calls[0]["ttl"], 3600)
        self.assertEqual(self.calls[0]["headers"], {"Urgency": "high"})
        state = json.loads(self.service.path.read_text())
        self.assertEqual(len(state["subscriptions"]), 1)

    def test_event_delivery_rejects_non_event_and_external_urls(self):
        self.service.subscribe(subscription())
        for url in ("/events", "https://example.com/events?event=1", "//evil/events?event=1"):
            with self.subTest(url=url), self.assertRaises(PushNotificationError):
                self.service.send_event(
                    title="Camera activity",
                    body="Tap to view.",
                    url=url,
                    tag="ose-event-test",
                    contact_uri="https://osecameras.example.ts.net",
                )
