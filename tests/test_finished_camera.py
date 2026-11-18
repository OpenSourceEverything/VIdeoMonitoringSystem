import unittest

from video_monitoring_system.camera_setup import CameraSetupBinding
from video_monitoring_system.finished_camera import (
    FinishedCameraError,
    FinishedCameraEvidence,
    FinishedCameraSetupTransport,
    QualifiedFinishedCamera,
)


class Driver:
    def __init__(self, evidence):
        self.evidence = evidence
        self.bindings = []

    def verify(self, binding):
        self.bindings.append(binding)
        return self.evidence


class FinishedCameraTests(unittest.TestCase):
    def setUp(self):
        self.binding = CameraSetupBinding(
            transaction_id="transaction-example-12345678",
            camera_id="CAM-0123456789ABCDEF",
            recorder_id="REC-0123456789ABCDEF",
            bootstrap_identity="sha256:" + "a" * 64,
            permanent_identity="identity:camera-public-key-1234",
            transport="finished-camera",
        )
        self.profile = QualifiedFinishedCamera(
            manufacturer="Example Camera Company",
            model="Outdoor WiFi 5MP",
            hardware_revision="V2.20",
            firmware_versions=("1.2.3",),
            wifi_bands_ghz=(2.4, 5.0),
        )
        self.evidence = FinishedCameraEvidence(
            manufacturer=self.profile.manufacturer,
            model=self.profile.model,
            hardware_revision=self.profile.hardware_revision,
            firmware_version="1.2.3",
            permanent_identity=self.binding.permanent_identity,
            main_codec="h264",
            detection_codec="h264",
            main_stream_advancing=True,
            detection_stream_advancing=True,
            cloud_blocked=True,
        )

    def test_exact_local_h264_camera_completes_common_enrollment(self):
        driver = Driver(self.evidence)
        adapter = FinishedCameraSetupTransport(self.profile, driver)

        result = adapter.verified_enrollment(self.binding)

        self.assertEqual(result.camera_id, self.binding.camera_id)
        self.assertEqual(result.permanent_identity, self.binding.permanent_identity)
        self.assertEqual(driver.bindings, [self.binding])

    def test_revision_stream_or_cloud_mismatch_fails_closed(self):
        for changed in (
            {"hardware_revision": "V2.21"},
            {"main_codec": "h265"},
            {"detection_stream_advancing": False},
            {"cloud_blocked": False},
        ):
            values = {
                field: getattr(self.evidence, field)
                for field in self.evidence.__dataclass_fields__
            }
            values.update(changed)
            adapter = FinishedCameraSetupTransport(
                self.profile,
                Driver(FinishedCameraEvidence(**values)),
            )
            with self.subTest(changed=changed), self.assertRaises(
                FinishedCameraError
            ):
                adapter.verified_enrollment(self.binding)


if __name__ == "__main__":
    unittest.main()
