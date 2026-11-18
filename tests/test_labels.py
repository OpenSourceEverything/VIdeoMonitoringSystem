import tempfile
import unittest
from pathlib import Path

try:
    import segno  # noqa: F401
except ImportError:
    segno = None

from video_monitoring_system.labels import (
    ClaimPayloadError,
    camera_code,
    claim_payload,
    parse_camera_code,
    parse_claim_payload,
    write_claim_label,
)
from video_monitoring_system.provisioning import ProvisioningJob, ProvisioningSpec


class LabelTests(unittest.TestCase):
    def test_payload_contains_claim_material_but_not_private_key(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1), wifi_country="US"
        ).identity.cameras[0]
        payload = claim_payload(camera)
        self.assertTrue(payload.startswith("http://osecameras.local/#add-camera?"))
        self.assertIn(camera.device_id, payload)
        self.assertIn(camera.claim_secret, payload)
        self.assertNotIn(camera.key.private_identity, payload)
        self.assertNotIn(camera.stream_secret, payload)
        parsed = parse_claim_payload(payload)
        self.assertEqual(parsed.kit_id, camera.kit_id)
        self.assertEqual(parsed.device_id, camera.device_id)
        self.assertEqual(parsed.camera_number, camera.camera_number)
        self.assertEqual(parsed.claim_code, camera.claim_secret)
        printed = camera_code(camera)
        self.assertEqual(parse_camera_code(printed), camera.claim_secret)
        self.assertNotIn(camera.stream_secret, printed)

    def test_legacy_payload_is_accepted_but_malformed_labels_fail_closed(self) -> None:
        legacy = (
            "vms://claim?kit=KIT-0123456789AB&device=CAM-0123456789ABCDEF"
            "&number=16&code=abcdefghijklmnopqrstuvwxyz012345"
        )
        self.assertEqual(parse_claim_payload(legacy).camera_number, 16)
        for value in (
            "",
            "https://evil.example/#add-camera?" + legacy.partition("?")[2],
            legacy + "&extra=value",
            legacy.replace("number=16", "number=17"),
            legacy.replace("code=", "code=short&ignored="),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ClaimPayloadError):
                    parse_claim_payload(value)

    @unittest.skipIf(segno is None, "segno is not installed")
    def test_svg_label_is_written(self) -> None:
        camera = ProvisioningJob.create(
            ProvisioningSpec(camera_count=1), wifi_country="US"
        ).identity.cameras[0]
        with tempfile.TemporaryDirectory() as directory:
            path = write_claim_label(camera, Path(directory))
            self.assertTrue(path.is_file())
            content = path.read_text(encoding="utf-8")
            self.assertIn("<svg", content)
            self.assertIn("Camera code", content)
            groups = camera_code(camera).split()
            self.assertIn(" ".join(groups[:4]), content)
            self.assertIn(" ".join(groups[4:]), content)

    def test_manual_camera_code_rejects_altered_or_ambiguous_input(self) -> None:
        for value in ("", "too short", "ABCD/EFGH/IJKL/MNOP", "A" * 129):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ClaimPayloadError, "Camera code is invalid"):
                    parse_camera_code(value)


if __name__ == "__main__":
    unittest.main()
