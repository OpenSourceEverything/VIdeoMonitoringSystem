from __future__ import annotations

import base64
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from video_monitoring_system.imaging import (
    ConfirmationError,
    DevelopmentImageNotAllowed,
    DiskTarget,
    ImageReleaseManifest,
    ImagingError,
    ImagingResult,
    ImagerNotFoundError,
    InvalidReleaseSignature,
    SignatureMetadata,
    UntrustedReleaseKey,
    UnsafeDiskError,
    build_imager_argv,
    discover_rpi_imager,
    image_media,
    load_release_manifest,
)
from video_monitoring_system.provisioning import BootCustomization

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # pragma: no cover - exercised in dependency-light environments
    Ed25519PrivateKey = None


class FakeDiskDiscovery:
    def __init__(self, disks: list[DiskTarget]) -> None:
        self.disks = disks
        self.calls = 0

    def list_disks(self) -> list[DiskTarget]:
        self.calls += 1
        return list(self.disks)


class FakeCompletedProcess:
    returncode = 0


def signature() -> SignatureMetadata:
    return SignatureMetadata(
        algorithm="ed25519",
        key_id="release-key-2026",
        signature=base64.b64encode(bytes(64)).decode("ascii"),
    )


def manifest(
    *, channel: str = "product", with_signature: bool = True
) -> ImageReleaseManifest:
    return ImageReleaseManifest(
        schema_version=1,
        release_version="1.2.3",
        role="camera",
        supported_boards=("zero-w", "zero-2-w"),
        image="https://updates.example.invalid/camera-1.2.3.img.xz",
        sha256="a" * 64,
        signature=signature() if with_signature else None,
        channel=channel,
        architecture="armhf",
        bits=32,
        released="2026-06-18",
        extract_sha256="b" * 64,
    )


def signed_manifest() -> tuple[ImageReleaseManifest, bytes]:
    if Ed25519PrivateKey is None:
        raise RuntimeError("cryptography is unavailable")
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    unsigned = manifest()
    release = replace(
        unsigned,
        signature=SignatureMetadata(
            algorithm="ed25519",
            key_id="release-key-2026",
            signature=base64.b64encode(
                private_key.sign(unsigned.canonical_metadata())
            ).decode("ascii"),
        ),
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return release, public_key


def target(**changes: object) -> DiskTarget:
    values: dict[str, object] = {
        "device": "/dev/example-card",
        "stable_id": "CARD-1234",
        "display_name": "32 GB media",
        "size_bytes": 32_000_000_000,
        "removable": True,
        "system_disk": False,
    }
    values.update(changes)
    return DiskTarget(**values)


class ManifestTests(unittest.TestCase):
    def test_product_manifest_requires_signature_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "require signature"):
            manifest(with_signature=False)

    def test_development_manifest_requires_explicit_use_opt_in(self) -> None:
        release = manifest(channel="development", with_signature=False)

        with self.assertRaises(DevelopmentImageNotAllowed):
            release.validate_for_use(role="camera", board="zero-w")

        release.validate_for_use(
            role="camera", board="zero-w", allow_development=True
        )

    def test_product_manifest_requires_a_trusted_key_before_use(self) -> None:
        with self.assertRaises(UntrustedReleaseKey):
            manifest().validate_for_use(role="camera", board="zero-w")

    @unittest.skipIf(Ed25519PrivateKey is None, "cryptography is not installed")
    def test_product_manifest_signature_is_cryptographically_verified(self) -> None:
        release, public_key = signed_manifest()

        release.validate_for_use(
            role="camera",
            board="zero-w",
            trusted_release_keys={"release-key-2026": public_key},
        )
        release.validate_for_use(
            role="camera",
            board="zero-2-w",
            trusted_release_keys={
                "release-key-2026": base64.b64encode(public_key).decode("ascii")
            },
        )

        tampered = replace(release, sha256="b" * 64)
        with self.assertRaises(InvalidReleaseSignature):
            tampered.validate_for_use(
                role="camera",
                board="zero-w",
                trusted_release_keys={"release-key-2026": public_key},
            )

    def test_signature_metadata_is_strict(self) -> None:
        with self.assertRaisesRegex(ValueError, "algorithm"):
            SignatureMetadata("rsa", "release-key-2026", signature().signature)
        with self.assertRaisesRegex(ValueError, "64 bytes"):
            SignatureMetadata(
                "ed25519",
                "release-key-2026",
                base64.b64encode(bytes(63)).decode("ascii"),
            )

    def test_manifest_rejects_wrong_role_board_channel_and_digest(self) -> None:
        release = manifest()
        with self.assertRaisesRegex(ValueError, "does not match"):
            release.validate_for_use(role="recorder", board="zero-w")
        with self.assertRaisesRegex(ValueError, "does not support"):
            release.validate_for_use(role="camera", board="pi4")
        with self.assertRaisesRegex(ValueError, "channel"):
            ImageReleaseManifest(
                schema_version=1,
                release_version="1",
                role="camera",
                supported_boards=("zero-w",),
                image="image.img",
                sha256="a" * 64,
                signature=signature(),
                channel="nightly",
                architecture="armhf",
                bits=32,
                released="2026-06-18",
            )
        with self.assertRaisesRegex(ValueError, "64 lowercase"):
            ImageReleaseManifest(
                schema_version=1,
                release_version="1",
                role="camera",
                supported_boards=("zero-w",),
                image="image.img",
                sha256="A" * 64,
                signature=signature(),
                channel="product",
                architecture="armhf",
                bits=32,
                released="2026-06-18",
            )

    def test_manifest_schema_and_supported_boards_are_strict(self) -> None:
        values = manifest().to_dict()
        values["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "schema_version"):
            ImageReleaseManifest.from_dict(values)

        values = manifest().to_dict()
        values["supported_boards"] = ["zero-w", "zero-w"]
        with self.assertRaisesRegex(ValueError, "duplicates"):
            ImageReleaseManifest.from_dict(values)

        values = manifest().to_dict()
        values["surprise"] = True
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ImageReleaseManifest.from_dict(values)

    def test_json_compatible_manifest_round_trip(self) -> None:
        original = manifest()
        restored = ImageReleaseManifest.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_canonical_metadata_is_stable_and_excludes_signature_bytes(self) -> None:
        release = manifest()
        content = release.canonical_metadata()

        self.assertEqual(content, release.canonical_metadata())
        self.assertNotIn(release.signature.signature.encode("ascii"), content)
        self.assertIn(b'"architecture":"armhf"', content)
        self.assertIn(b'"bits":32', content)

    def test_public_pilot_manifest_is_explicitly_unsigned_development(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "releases"
            / "pilot-camera-raspios-2026-06-18.json"
        )
        release = load_release_manifest(path)

        self.assertEqual(release.channel, "development")
        self.assertIsNone(release.signature)
        self.assertEqual(release.architecture, "armhf")
        self.assertEqual(release.bits, 32)
        self.assertEqual(release.released, "2026-06-18")
        self.assertEqual(release.supported_boards, ("zero-w", "zero-2-w"))
        self.assertEqual(
            release.image,
            "https://downloads.raspberrypi.com/raspios_lite_armhf/images/"
            "raspios_lite_armhf-2026-06-19/"
            "2026-06-18-raspios-trixie-armhf-lite.img.xz",
        )
        self.assertEqual(
            release.sha256,
            "ea4e84c501d6dd4f4b1d04eb84df133a03f90a05ee2e8ab849185c17c2b0707b",
        )
        with self.assertRaises(DevelopmentImageNotAllowed):
            release.validate_for_use(role="camera", board="zero-w")
        release.validate_for_use(
            role="camera", board="zero-w", allow_development=True
        )


class ImagerCommandTests(unittest.TestCase):
    def test_executable_is_discovered_with_expected_program_name(self) -> None:
        names: list[str] = []

        def which(name: str) -> str | None:
            names.append(name)
            return "/opt/tools/rpi-imager"

        self.assertEqual(
            discover_rpi_imager(which=which), "/opt/tools/rpi-imager"
        )
        self.assertEqual(names, ["rpi-imager"])

    def test_missing_executable_has_a_clear_error(self) -> None:
        with self.assertRaises(ImagerNotFoundError):
            discover_rpi_imager(which=lambda _: None, candidates=())

    def test_standard_location_is_used_when_not_on_search_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "rpi-imager"
            executable.write_bytes(b"tool")

            self.assertEqual(
                discover_rpi_imager(
                    which=lambda _: None,
                    candidates=(executable,),
                ),
                str(executable),
            )

    def test_argv_uses_verification_and_cloud_init_without_a_shell(self) -> None:
        argv = build_imager_argv(
            executable="/tools/rpi-imager",
            manifest=manifest(),
            target=target(),
            user_data_path="/private/user-data",
            network_config_path="/private/network-config",
        )

        self.assertEqual(
            argv,
            (
                "/tools/rpi-imager",
                "--cli",
                "--sha256",
                "b" * 64,
                "--cloudinit-userdata",
                "/private/user-data",
                "--cloudinit-networkconfig",
                "/private/network-config",
                "https://updates.example.invalid/camera-1.2.3.img.xz",
                "/dev/example-card",
            ),
        )
        self.assertNotIn("--disable-verify", argv)

    def test_imager_requires_the_extracted_image_hash(self) -> None:
        with self.assertRaisesRegex(ImagingError, "extracted-image checksum"):
            build_imager_argv(
                executable="/tools/rpi-imager",
                manifest=replace(manifest(), extract_sha256=None),
                target=target(),
                user_data_path="/private/user-data",
                network_config_path="/private/network-config",
            )


class ImagingTests(unittest.TestCase):
    user_data = "#cloud-config\nwrite_files:\n  - content: private-device-key\n"
    network_config = "version: 2\nsecret: private-wifi-passphrase\n"

    def call(self, **changes: object):
        arguments: dict[str, object] = {
            "manifest": manifest(channel="development", with_signature=False),
            "role": "camera",
            "board": "zero-w",
            "target": target(),
            "user_data": self.user_data,
            "network_config": self.network_config,
            "allow_development": True,
            "which": lambda _: "/tools/rpi-imager",
        }
        arguments.update(changes)
        return image_media(**arguments)

    def test_dry_run_is_redacted_and_has_no_disk_side_effects(self) -> None:
        discovery = FakeDiskDiscovery([target()])
        runner_called = False

        def runner(*args: object, **kwargs: object) -> FakeCompletedProcess:
            nonlocal runner_called
            runner_called = True
            return FakeCompletedProcess()

        plan = self.call(disk_discovery=discovery, runner=runner)

        self.assertTrue(plan.redacted)
        self.assertEqual(plan.required_confirmation, "ERASE CARD-1234")
        self.assertIn("<owner-private-user-data>", plan.argv)
        self.assertIn("<owner-private-network-config>", plan.argv)
        self.assertNotIn("private-device-key", repr(plan))
        self.assertNotIn("private-wifi-passphrase", repr(plan))
        self.assertEqual(discovery.calls, 0)
        self.assertFalse(runner_called)

    def test_actual_write_requires_discovery_and_exact_confirmation(self) -> None:
        with self.assertRaisesRegex(UnsafeDiskError, "require disk discovery"):
            self.call(dry_run=False, confirmation="ERASE CARD-1234")

        discovery = FakeDiskDiscovery([target()])
        with self.assertRaises(ConfirmationError):
            self.call(
                dry_run=False,
                confirmation="erase card-1234",
                disk_discovery=discovery,
            )

    def test_actual_write_rejects_unproven_nonremovable_and_system_targets(self) -> None:
        cases = (
            FakeDiskDiscovery([]),
            FakeDiskDiscovery([target(removable=False)]),
            FakeDiskDiscovery([target(system_disk=True)]),
        )
        for discovery in cases:
            with self.subTest(disks=discovery.disks):
                with self.assertRaises(UnsafeDiskError):
                    self.call(
                        dry_run=False,
                        confirmation="ERASE CARD-1234",
                        disk_discovery=discovery,
                    )

    def test_actual_write_rejects_changed_or_ambiguous_identity(self) -> None:
        changed = FakeDiskDiscovery([target(size_bytes=64_000_000_000)])
        duplicate = FakeDiskDiscovery(
            [target(), target(device="/dev/second-reader")]
        )
        for discovery in (changed, duplicate):
            with self.subTest(disks=discovery.disks):
                with self.assertRaises(UnsafeDiskError):
                    self.call(
                        dry_run=False,
                        confirmation="ERASE CARD-1234",
                        disk_discovery=discovery,
                    )

    def test_actual_write_uses_private_temporary_files_and_argument_list(self) -> None:
        observed: dict[str, object] = {}

        def runner(argv: tuple[str, ...], **kwargs: object) -> FakeCompletedProcess:
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            user_path = Path(argv[5])
            network_path = Path(argv[7])
            observed["paths"] = (user_path, network_path)
            observed["contents"] = (
                user_path.read_text(encoding="utf-8"),
                network_path.read_text(encoding="utf-8"),
            )
            observed["modes"] = (
                stat.S_IMODE(user_path.stat().st_mode),
                stat.S_IMODE(network_path.stat().st_mode),
            )
            return FakeCompletedProcess()

        result = self.call(
            dry_run=False,
            confirmation="ERASE CARD-1234",
            disk_discovery=FakeDiskDiscovery([target()]),
            runner=runner,
        )

        self.assertIsInstance(result, ImagingResult)
        self.assertEqual(result.returncode, 0)
        argv = observed["argv"]
        self.assertIsInstance(argv, tuple)
        self.assertNotIn("--disable-verify", argv)
        self.assertEqual(observed["kwargs"], {"check": True, "shell": False})
        self.assertEqual(
            observed["contents"], (self.user_data, self.network_config)
        )
        if os.name == "posix":
            for mode in observed["modes"]:
                self.assertEqual(mode & 0o077, 0)
        for path in observed["paths"]:
            self.assertFalse(path.exists())
        self.assertNotIn("private-device-key", repr(result))
        self.assertNotIn("private-wifi-passphrase", repr(result))

    def test_boot_customization_is_explicit_and_delegated_after_write(self) -> None:
        customization = BootCustomization(
            config_txt_append=("enable_uart=1",),
            cmdline_append=("console=serial0,115200",),
            cmdline_remove=("quiet",),
        )
        events: list[str] = []

        def runner(*args: object, **kwargs: object) -> FakeCompletedProcess:
            events.append("image")
            return FakeCompletedProcess()

        def customize(selected: DiskTarget, requested: BootCustomization) -> None:
            self.assertEqual(selected, target())
            self.assertEqual(requested, customization)
            events.append("boot")

        preview = self.call(boot_customization=customization)
        self.assertEqual(preview.boot_customization, "first-boot-fallback")

        result = self.call(
            boot_customization=customization,
            boot_customizer=customize,
            dry_run=False,
            confirmation="ERASE CARD-1234",
            disk_discovery=FakeDiskDiscovery([target()]),
            runner=runner,
        )

        self.assertEqual(events, ["image", "boot"])
        self.assertEqual(result.plan.boot_customization, "adapter")

    def test_runner_failure_propagates_and_temporary_files_are_removed(self) -> None:
        paths: list[Path] = []

        def runner(argv: tuple[str, ...], **kwargs: object) -> FakeCompletedProcess:
            paths.extend((Path(argv[5]), Path(argv[7])))
            raise subprocess.CalledProcessError(7, argv)

        with self.assertRaises(subprocess.CalledProcessError):
            self.call(
                dry_run=False,
                confirmation="ERASE CARD-1234",
                disk_discovery=FakeDiskDiscovery([target()]),
                runner=runner,
            )

        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_development_write_needs_allow_flag_even_for_dry_run(self) -> None:
        development = manifest(channel="development", with_signature=False)
        with self.assertRaises(DevelopmentImageNotAllowed):
            self.call(manifest=development, allow_development=False)

        plan = self.call(manifest=development, allow_development=True)
        self.assertEqual(plan.channel, "development")


class DiskTargetTests(unittest.TestCase):
    def test_confirmation_is_stable_id_specific(self) -> None:
        self.assertEqual(target().confirmation, "ERASE CARD-1234")

    def test_invalid_disk_metadata_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            target(size_bytes=0)
        with self.assertRaises(TypeError):
            target(removable=1)
        with self.assertRaises(ValueError):
            target(stable_id="bad\nidentity")


if __name__ == "__main__":
    unittest.main()
