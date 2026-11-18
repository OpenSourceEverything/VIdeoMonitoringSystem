from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import video_monitoring_system.image_build as image_build
import video_monitoring_system.recorder_image_build as recorder_build
from video_monitoring_system.provisioning import (
    PREINSTALLED_RECORDER_IMAGE_REFERENCE,
)
from video_monitoring_system.recorder_image_build import (
    PythonWheel,
    assemble_pi_gen_stage,
    load_build_manifest,
    prepare_wheelhouse,
)


REPOSITORY = Path(__file__).resolve().parents[1]
MANIFEST = (
    REPOSITORY
    / "platforms"
    / "raspberry-pi"
    / "recorder-image"
    / "pilot"
    / "build.json"
)


class RecorderImageBuildTests(unittest.TestCase):
    def test_manifest_pins_the_generic_pi4_runtime(self) -> None:
        manifest = load_build_manifest(MANIFEST)

        self.assertEqual(
            manifest.image_reference, PREINSTALLED_RECORDER_IMAGE_REFERENCE
        )
        self.assertEqual(manifest.architecture, "arm64")
        self.assertEqual(manifest.supported_boards, ("pi4",))
        self.assertNotIn("dnsmasq", manifest.packages)
        self.assertNotIn("rfkill", manifest.packages)
        self.assertEqual(len(manifest.builder_commit), 40)
        self.assertEqual(
            manifest.builder_commit,
            "ca8aeed0ae300c2a89f55ce9617d5f96a27e99e5",
        )
        self.assertEqual(
            {wheel.name.lower() for wheel in manifest.python_wheels},
            {"cryptography", "pyyaml", "segno", "cffi", "pycparser", "setuptools"},
        )

    def test_arm64_builder_and_emulation_are_checked(self) -> None:
        manifest = load_build_manifest(MANIFEST)
        patches, _pinned_hash, _patched_hash = (
            image_build._pi_gen_compatibility_inputs(manifest.architecture)
        )
        binfmt_patch = REPOSITORY / patches[1]
        content = binfmt_patch.read_text(encoding="utf-8")

        self.assertIn("--install arm64", content)
        self.assertGreaterEqual(content.count("arch-test arm64"), 3)
        with tempfile.TemporaryDirectory() as directory:
            pi_gen = Path(directory)
            (pi_gen / "build.sh").write_text(
                "export ARCH=armhf\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                recorder_build.RecorderImageBuildError,
                "does not build arm64",
            ):
                image_build._verify_pi_gen_architecture(pi_gen, "arm64")
            (pi_gen / "build.sh").write_text(
                "export ARCH=arm64\n", encoding="utf-8"
            )
            image_build._verify_pi_gen_architecture(pi_gen, "arm64")

    def test_verified_wheel_cache_is_reused_without_network(self) -> None:
        manifest = load_build_manifest(MANIFEST)
        with tempfile.TemporaryDirectory() as directory:
            wheelhouse = Path(directory)
            wheels = []
            for wheel in manifest.python_wheels:
                content = wheel.filename.encode("ascii")
                (wheelhouse / wheel.filename).write_bytes(content)
                wheels.append(
                    replace(wheel, sha256=hashlib.sha256(content).hexdigest())
                )
            test_manifest = replace(manifest, python_wheels=tuple(wheels))
            with mock.patch.object(recorder_build.subprocess, "run") as run:
                self.assertEqual(
                    prepare_wheelhouse(test_manifest, wheelhouse), wheelhouse
                )
            run.assert_not_called()

    def test_stage_contains_public_runtime_but_no_kit_state(self) -> None:
        manifest = load_build_manifest(MANIFEST)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheels"
            wheelhouse.mkdir()
            wheels: list[PythonWheel] = []
            for wheel in manifest.python_wheels:
                content = wheel.filename.encode("ascii")
                (wheelhouse / wheel.filename).write_bytes(content)
                wheels.append(
                    replace(wheel, sha256=hashlib.sha256(content).hexdigest())
                )
            test_manifest = replace(manifest, python_wheels=tuple(wheels))
            pi_gen = root / "pi-gen"
            (pi_gen / "stage2").mkdir(parents=True)
            stage = pi_gen / "stage-vms-recorder"

            assemble_pi_gen_stage(
                REPOSITORY,
                stage,
                test_manifest,
                wheelhouse,
                source_revision="a" * 40,
            )

            files = stage / "00-vms-recorder" / "files"
            contract = json.loads(
                (
                    files
                    / "usr"
                    / "share"
                    / "vms-recorder-image"
                    / "build-contract.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(contract["role"], "recorder")
            self.assertFalse(contract["contains_kit_secrets"])
            self.assertEqual(contract["vms_source_commit"], "a" * 40)
            self.assertTrue(
                (files / "usr/src/video-monitoring-system/pyproject.toml").is_file()
            )
            self.assertTrue(
                (
                    files
                    / "usr/src/video-monitoring-system"
                    / "platforms/linux/recorder/install-recorder"
                ).is_file()
            )
            self.assertTrue(
                os.access(
                    files
                    / "usr/src/video-monitoring-system"
                    / "platforms/linux/recorder/install-recorder",
                    os.X_OK,
                )
            )
            self.assertFalse((files / "etc/vms/device.json").exists())
            self.assertFalse(
                (files / "usr/local/libexec/vms-first-boot").exists()
            )

    def test_cli_uses_the_versioned_manifest_by_default(self) -> None:
        with (
            mock.patch.object(
                recorder_build,
                "_require_clean_versioned_source",
                return_value="b" * 40,
            ),
            mock.patch.object(recorder_build, "check_build_host"),
            mock.patch.object(
                recorder_build,
                "load_build_manifest",
                wraps=load_build_manifest,
            ) as loader,
        ):
            self.assertEqual(recorder_build.main(["--check"]), 0)

        loader.assert_called_once_with(MANIFEST)


if __name__ == "__main__":
    unittest.main()
