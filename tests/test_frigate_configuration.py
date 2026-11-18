import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import yaml

from video_monitoring_system.frigate_configuration import (
    FrigateCameraPolicy,
    FrigateCameraSource,
    FrigateConfigurationError,
    load_camera_policies,
    save_camera_policies,
    write_managed_frigate_configuration,
)


class ManagedFrigateConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = FrigateCameraSource(
            camera_id="CAM-0123456789ABCDEF",
            engine_name="back_porch",
            stream_url=(
                "http://CAM-0123456789ABCDEF:private-stream-secret@"
                "10.23.4.5:9080/stream.mjpg"
            ),
        )

    def test_writes_idempotent_private_runtime_without_secret_in_yaml(self):
        first = write_managed_frigate_configuration(
            self.root / "frigate",
            (self.source,),
            candidates=("192.168.1.10:8555", "100.64.0.10:8555"),
        )
        second = write_managed_frigate_configuration(
            self.root / "frigate",
            (self.source,),
            candidates=("192.168.1.10:8555", "100.64.0.10:8555"),
        )

        config_text = first.configuration_path.read_text(encoding="utf-8")
        environment = first.environment_path.read_text(encoding="utf-8")
        config = yaml.safe_load(config_text)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(first.cameras, 1)
        self.assertNotIn("private-stream-secret", config_text)
        self.assertIn("private-stream-secret", environment)
        self.assertEqual(
            config["cameras"]["back_porch"]["detect"],
            {"enabled": True, "width": 640, "height": 360, "fps": 5},
        )
        self.assertEqual(
            config["cameras"]["back_porch"]["ffmpeg"]["inputs"][0]["path"],
            "rtsp://127.0.0.1:8554/back_porch",
        )
        self.assertEqual(
            config["go2rtc"]["webrtc"]["candidates"],
            [
                "192.168.1.10:8555",
                "100.64.0.10:8555",
            ],
        )
        self.assertNotIn("FRIGATE_OSE_WEBRTC_CANDIDATE", environment)
        self.assertEqual(
            config["go2rtc"]["ffmpeg"]["ose_mjpeg_10fps"],
            "-r 10 -fflags nobuffer -flags low_delay -i {input}",
        )
        self.assertEqual(
            config["go2rtc"]["streams"]["back_porch"],
            "ffmpeg:{FRIGATE_OSE_CAMERA_F1C54BE68592E567_URL}"
            "#input=ose_mjpeg_10fps#video=ose_h264_software_10fps",
        )
        self.assertEqual(config["version"], "0.18-0")
        software = config["go2rtc"]["ffmpeg"]["ose_h264_software_10fps"]
        self.assertIn("-preset:v superfast", software)
        self.assertIn("-threads:v 2 -g 20 -bf 0", software)
        self.assertEqual(config["record"]["motion"]["days"], 7)
        self.assertEqual(config["record"]["alerts"]["retain"]["days"], 7)
        self.assertEqual(first.configuration_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(first.environment_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "frigate").stat().st_mode & 0o777, 0o700)

    def test_frigate_yaml_normalization_does_not_trigger_a_restart(self):
        first = write_managed_frigate_configuration(
            self.root / "frigate",
            (self.source,),
            candidates=("192.168.0.108:8555",),
        )
        normalized = "# Frigate normalized this file.\n" + first.configuration_path.read_text(
            encoding="utf-8"
        )
        first.configuration_path.write_text(normalized, encoding="utf-8")

        second = write_managed_frigate_configuration(
            self.root / "frigate",
            (self.source,),
            candidates=("192.168.0.108:8555",),
        )

        self.assertFalse(second.changed)
        self.assertEqual(
            second.configuration_path.read_text(encoding="utf-8"), normalized
        )

    def test_native_h264_camera_is_copied_without_recorder_transcoding(self):
        source = FrigateCameraSource(
            camera_id=self.source.camera_id,
            engine_name=self.source.engine_name,
            stream_url=(
                "http://CAM-0123456789ABCDEF:private-stream-secret@"
                "10.23.4.5:9080/stream.h264"
            ),
            codec="h264",
            frame_rate=15,
        )

        result = write_managed_frigate_configuration(
            self.root / "frigate-h264",
            (source,),
            candidates=("192.168.1.10:8555",),
        )

        config = yaml.safe_load(result.configuration_path.read_text(encoding="utf-8"))
        stream = config["go2rtc"]["streams"]["back_porch"]
        self.assertEqual(stream, "{FRIGATE_OSE_CAMERA_F1C54BE68592E567_URL}")
        self.assertEqual(config["go2rtc"]["ffmpeg"], {})
        self.assertNotIn("ffmpeg:", stream)
        self.assertNotIn("libx264", result.configuration_path.read_text())
        self.assertIn("/stream.h264", result.environment_path.read_text())

    def test_recording_pause_changes_engine_policy_without_disabling_live_or_detection(self):
        paused = replace(self.source, recording_enabled=False)
        result = write_managed_frigate_configuration(
            self.root / "paused", (paused,), candidates=("192.168.1.10:8555",),
        )
        config = yaml.safe_load(result.configuration_path.read_text())
        camera = config["cameras"]["back_porch"]
        self.assertEqual(camera["record"], {"enabled": False})
        self.assertTrue(camera["detect"]["enabled"])
        self.assertIn("back_porch", config["go2rtc"]["streams"])
        with self.assertRaises(FrigateConfigurationError):
            write_managed_frigate_configuration(
                self.root / "invalid-pause", (replace(self.source, recording_enabled="false"),),
                candidates=("192.168.1.10:8555",),
            )

    def test_pi4_hardware_encoder_is_explicit_and_only_used_for_mjpeg(self):
        result = write_managed_frigate_configuration(
            self.root / "frigate-hardware",
            (self.source,),
            candidates=("192.168.1.10:8555",),
            mjpeg_hardware="v4l2m2m",
        )

        config = yaml.safe_load(result.configuration_path.read_text(encoding="utf-8"))
        self.assertEqual(
            config["go2rtc"]["ffmpeg"]["ose_h264_v4l2m2m"],
            "-pix_fmt:v yuv420p -c:v h264_v4l2m2m -g 50 -bf 0",
        )
        self.assertTrue(
            config["go2rtc"]["streams"]["back_porch"].endswith(
                "#video=ose_h264_v4l2m2m"
            )
        )

    def test_software_encoder_cost_can_be_bounded_without_changing_video_dimensions(self):
        result = write_managed_frigate_configuration(
            self.root / "software",
            (self.source,),
            candidates=("192.168.1.10:8555",),
            mjpeg_software_preset="ultrafast",
        )
        config = yaml.safe_load(result.configuration_path.read_text())
        software = config["go2rtc"]["ffmpeg"]["ose_h264_software_10fps"]
        self.assertIn("-preset:v ultrafast", software)
        self.assertIn("-crf:v 23", software)
        self.assertNotIn("scale", software)
        self.assertEqual(config["cameras"]["back_porch"]["detect"]["fps"], 5)
        with self.assertRaises(FrigateConfigurationError):
            write_managed_frigate_configuration(
                self.root / "bad-software", (self.source,),
                candidates=("192.168.1.10:8555",), mjpeg_software_preset="arbitrary",
            )

        with self.assertRaisesRegex(FrigateConfigurationError, "hardware"):
            write_managed_frigate_configuration(
                self.root / "frigate-invalid-hardware",
                (self.source,),
                candidates=("192.168.1.10:8555",),
                mjpeg_hardware="unknown",
            )

    def test_accepts_only_the_matching_stable_private_relay_source(self):
        source = FrigateCameraSource(
            camera_id=self.source.camera_id,
            engine_name=self.source.engine_name,
            stream_url=(
                "http://ose-camera-route-relay.internal:9081/v1/cameras/"
                f"{self.source.camera_id}/active/main"
            ),
            codec="h264",
            frame_rate=15,
        )

        result = write_managed_frigate_configuration(
            self.root / "frigate-relay",
            (source,),
            candidates=("192.168.1.10:8555",),
        )

        environment = result.environment_path.read_text(encoding="utf-8")
        self.assertIn("ose-camera-route-relay.internal:9081", environment)
        self.assertNotIn("private-stream-secret", environment)

        mismatched = FrigateCameraSource(
            camera_id=self.source.camera_id,
            engine_name=self.source.engine_name,
            stream_url=(
                "http://ose-camera-route-relay.internal:9081/v1/cameras/"
                "CAM-FFFFFFFFFFFFFFFF/active/main"
            ),
            codec="h264",
            frame_rate=15,
        )
        with self.assertRaisesRegex(FrigateConfigurationError, "stream"):
            write_managed_frigate_configuration(
                self.root / "frigate-relay-mismatch",
                (mismatched,),
                candidates=("192.168.1.10:8555",),
            )

    def test_projects_product_retention_into_frigate(self):
        result = write_managed_frigate_configuration(
            self.root / "frigate",
            (self.source,),
            candidates=("192.168.0.108:8555",),
            retention_days=14,
        )

        config = yaml.safe_load(result.configuration_path.read_text(encoding="utf-8"))
        self.assertEqual(config["record"]["continuous"]["days"], 0)
        self.assertEqual(config["record"]["motion"]["days"], 14)
        self.assertEqual(config["record"]["alerts"]["retain"]["days"], 14)

    def test_validation_mode_records_continuously_for_one_day_only(self):
        result = write_managed_frigate_configuration(
            self.root / "validation-frigate",
            (self.source,),
            candidates=("192.168.0.108:8555",),
            validation_continuous_days=1,
        )
        config = yaml.safe_load(result.configuration_path.read_text(encoding="utf-8"))
        self.assertEqual(config["record"]["continuous"]["days"], 1)
        with self.assertRaisesRegex(FrigateConfigurationError, "Validation recording"):
            write_managed_frigate_configuration(
                self.root / "invalid-validation-frigate",
                (self.source,),
                candidates=("192.168.0.108:8555",),
                validation_continuous_days=2,
            )

    def test_finished_camera_rtsp_source_stays_private_and_requires_auth(self):
        source = FrigateCameraSource(
            camera_id=self.source.camera_id,
            engine_name="back_porch",
            stream_url="rtsp://camera:unique-secret@192.168.1.42:554/h264/main",
            codec="h264",
            frame_rate=15,
        )
        result = write_managed_frigate_configuration(
            self.root / "rtsp-frigate",
            (source,),
            candidates=("192.168.1.10:8555",),
        )
        text = result.configuration_path.read_text(encoding="utf-8")
        config = yaml.safe_load(text)
        self.assertNotIn("unique-secret", text)
        self.assertIn("unique-secret", result.environment_path.read_text(encoding="utf-8"))
        self.assertTrue(config["go2rtc"]["streams"]["back_porch"].startswith("{"))
        for bad_url in (
            "rtsp://192.168.1.42:554/h264/main",
            "rtsp://camera:secret@8.8.8.8:554/h264/main",
            "rtsp://camera:secret@192.168.1.42:invalid/h264/main",
        ):
            with self.subTest(bad_url=bad_url), self.assertRaises(FrigateConfigurationError):
                write_managed_frigate_configuration(
                    self.root / "bad-rtsp-frigate",
                    (FrigateCameraSource(
                        camera_id=source.camera_id,
                        engine_name="back_porch",
                        stream_url=bad_url,
                        codec="h264",
                    ),),
                    candidates=("192.168.1.10:8555",),
                )

    def test_private_policy_projects_ignore_zones_without_engine_vocabulary(self):
        policy_path = save_camera_policies(
            self.root,
            {
                self.source.camera_id: FrigateCameraPolicy(
                    contour_area=30,
                    ignore_zones=(
                        ("Moving vegetation", "0.0,0.0,0.6,0.0,0.5,0.7"),
                    ),
                )
            },
        )

        policies = load_camera_policies(self.root)
        result = write_managed_frigate_configuration(
            self.root / "frigate",
            (self.source,),
            candidates=("192.168.0.108:8555",),
            policies=policies,
        )

        config = yaml.safe_load(result.configuration_path.read_text(encoding="utf-8"))
        mask = config["cameras"]["back_porch"]["motion"]["mask"]
        self.assertEqual(mask["moving_vegetation"]["friendly_name"], "Moving vegetation")
        self.assertEqual(policy_path.stat().st_mode & 0o777, 0o600)

    def test_detection_rate_is_bounded_and_preserves_native_recording_stream(self):
        for invalid in (0, 11, True, 2.5):
            with self.assertRaises(FrigateConfigurationError):
                save_camera_policies(self.root,{self.source.camera_id: FrigateCameraPolicy(detect_fps=invalid)})
        save_camera_policies(self.root,{self.source.camera_id: FrigateCameraPolicy(detect_fps=3)})
        result = write_managed_frigate_configuration(self.root / "rate", (self.source,), candidates=("192.168.1.10:8555",),policies=load_camera_policies(self.root))
        config = yaml.safe_load(result.configuration_path.read_text())
        self.assertEqual(config["cameras"]["back_porch"]["detect"]["fps"],3)
        self.assertTrue(config["cameras"]["back_porch"]["record"]["enabled"])

    def test_rejects_unknown_policy_and_unsafe_candidate(self):
        policies = load_camera_policies(self.root)
        self.assertEqual(policies, {})
        with self.assertRaisesRegex(FrigateConfigurationError, "candidate"):
            write_managed_frigate_configuration(
                self.root / "frigate",
                (self.source,),
                candidates=("127.0.0.1:8555",),
            )
        with self.assertRaisesRegex(FrigateConfigurationError, "At least one"):
            write_managed_frigate_configuration(
                self.root / "frigate",
                (),
                candidates=("192.168.0.108:8555",),
            )

        invalid_rate = FrigateCameraSource(
            camera_id=self.source.camera_id,
            engine_name=self.source.engine_name,
            stream_url=self.source.stream_url,
            frame_rate=0,
        )
        with self.assertRaisesRegex(FrigateConfigurationError, "frame rate"):
            write_managed_frigate_configuration(
                self.root / "frigate-rate",
                (invalid_rate,),
                candidates=("192.168.0.108:8555",),
            )

    def test_rejects_unresolved_or_public_camera_stream(self):
        for hostname in ("camera.local", "8.8.8.8"):
            with self.subTest(hostname=hostname):
                source = FrigateCameraSource(
                    camera_id="CAM-0123456789ABCDEF",
                    engine_name="back_porch",
                    stream_url=f"http://CAM-1:secret@{hostname}:9080/stream.mjpg",
                )
                with self.assertRaises(FrigateConfigurationError):
                    write_managed_frigate_configuration(
                        self.root / f"frigate-{hostname}",
                        (source,),
                        candidates=("192.168.1.10:8555",),
                    )

    def test_rejects_codec_and_path_mismatch(self):
        source = FrigateCameraSource(
            camera_id=self.source.camera_id,
            engine_name=self.source.engine_name,
            stream_url=self.source.stream_url,
            codec="h264",
        )

        with self.assertRaisesRegex(FrigateConfigurationError, "stream"):
            write_managed_frigate_configuration(
                self.root / "frigate-codec-mismatch",
                (source,),
                candidates=("192.168.1.10:8555",),
            )

    @unittest.skipUnless(os.name == "posix", "POSIX permissions are required")
    def test_policy_must_be_private(self):
        path = self.root / "video-engine-policy.json"
        path.write_text('{"schema_version":1,"cameras":{}}', encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaises(FrigateConfigurationError):
            load_camera_policies(self.root)


if __name__ == "__main__":
    unittest.main()
