from __future__ import annotations

import json
import lzma
import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import video_monitoring_system.image_build as image_build_module

from video_monitoring_system.image_build import (
    CameraImageBuildError,
    CameraImageBuildManifest,
    RawImageInspection,
    RawImagePartition,
    _pi_gen_config,
    assemble_pi_gen_stage,
    build_camera_image,
    check_build_host,
    inspect_xz_image,
    load_build_manifest,
    verify_exported_image_rootfs,
    verify_rootfs_first_boot_state,
    verify_rootfs_runtime_contract,
)
from video_monitoring_system.provisioning import PREINSTALLED_CAMERA_IMAGE_REFERENCE


REPOSITORY = Path(__file__).resolve().parents[1]
PI_CAMERA_IMAGE = (
    REPOSITORY / "platforms" / "raspberry-pi" / "camera-image" / "pilot"
)
BUILD_MANIFEST = PI_CAMERA_IMAGE / "build.json"


class CameraImageBuildTests(unittest.TestCase):
    def test_cli_uses_the_versioned_manifest_by_default(self) -> None:
        with (
            mock.patch.object(
                image_build_module,
                "_require_clean_versioned_source",
                return_value="a" * 40,
            ),
            mock.patch.object(image_build_module, "check_build_host"),
            mock.patch.object(
                image_build_module,
                "load_build_manifest",
                wraps=load_build_manifest,
            ) as loader,
        ):
            self.assertEqual(image_build_module.main(["--check"]), 0)

        loader.assert_called_once_with(BUILD_MANIFEST)

    def test_versioned_manifest_declares_complete_preinstalled_runtime(self) -> None:
        manifest = load_build_manifest(BUILD_MANIFEST)

        self.assertEqual(manifest.image_reference, PREINSTALLED_CAMERA_IMAGE_REFERENCE)
        self.assertEqual(manifest.architecture, "armhf")
        self.assertEqual(manifest.supported_boards, ("zero-w", "zero-2-w"))
        self.assertIn("python3-picamera2", manifest.packages)
        self.assertIn("avahi-daemon", manifest.packages)
        self.assertIn("iputils-ping", manifest.packages)
        self.assertEqual(len(manifest.builder_commit), 40)
        self.assertEqual(manifest.release_version, "0.3.0-pilot.5")

    def test_manifest_fails_when_a_runtime_package_is_missing(self) -> None:
        value = json.loads(BUILD_MANIFEST.read_text(encoding="utf-8"))
        value["packages"].remove("python3-picamera2")

        with self.assertRaisesRegex(ValueError, "python3-picamera2"):
            CameraImageBuildManifest.from_dict(value)

    def test_stage_bakes_runtime_recovery_and_auditable_contract(self) -> None:
        manifest = load_build_manifest(BUILD_MANIFEST)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stage2").mkdir()
            stage = root / "stage-vms-camera"
            assemble_pi_gen_stage(
                REPOSITORY,
                stage,
                manifest,
                source_revision="a" * 40,
            )
            substage = stage / "00-vms-camera"
            contract_path = (
                substage
                / "files"
                / "usr"
                / "share"
                / "vms-camera-image"
                / "build-contract.json"
            )
            contract = json.loads(contract_path.read_text(encoding="utf-8"))

            self.assertEqual(contract["first_boot"], "configuration-only")
            self.assertNotIn("network_required_for_first_boot", contract)
            self.assertTrue(
                contract["network_requirements"]["private_lan_required_for_readiness"]
            )
            self.assertFalse(
                contract["network_requirements"]["internet_required_for_first_boot"]
            )
            self.assertFalse(
                contract["network_requirements"]["internet_required_for_readiness"]
            )
            self.assertEqual(
                contract["provisioning_dispatch"]["cloud_init_payload_stage"],
                "cloud-init-network.service",
            )
            self.assertEqual(
                contract["provisioning_dispatch"]["transaction_timeout_seconds"],
                600,
            )
            self.assertEqual(
                contract["post_cloud_init_cleanup"]["after"],
                "cloud-final.service",
            )
            self.assertEqual(
                set(contract["cloud_init_stage_output"]),
                {
                    "cloud-init-main.service",
                    "cloud-init-local.service",
                    "cloud-init-network.service",
                    "cloud-config.service",
                    "cloud-final.service",
                },
            )
            self.assertEqual(contract["vms_source_commit"], "a" * 40)
            self.assertRegex(contract["pi_gen_patch_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(contract["pi_gen_binfmt_patch_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                contract["pi_gen_first_boot_patch_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertIn("/opt/vms/camera_agent.py", contract["payload_sha256"])
            self.assertIn("/opt/vms/network_recovery.py", contract["payload_sha256"])
            self.assertEqual(
                contract["network_recovery"]["service"],
                "vms-network-recovery.service",
            )
            self.assertEqual(contract["network_recovery"]["interval_seconds"], 30)
            self.assertIn(
                "/usr/local/libexec/vms-provision", contract["payload_sha256"]
            )
            self.assertIn(
                "/usr/local/libexec/vms-provision-cleanup",
                contract["payload_sha256"],
            )
            self.assertTrue((stage / "EXPORT_IMAGE").is_file())
            self.assertTrue((root / "stage2" / "SKIP_IMAGES").is_file())
            self.assertIn("copy_previous", (stage / "prerun.sh").read_text())
            if os.name == "posix":
                self.assertTrue((stage / "prerun.sh").stat().st_mode & stat.S_IXUSR)
                self.assertTrue((substage / "00-run.sh").stat().st_mode & stat.S_IXUSR)
            self.assertTrue(
                (substage / "files" / "opt" / "vms" / "camera_agent.py").is_file()
            )
            self.assertTrue(
                (substage / "files" / "opt" / "vms" / "network_recovery.py").is_file()
            )
            ssh_config = (
                substage
                / "files"
                / "etc"
                / "ssh"
                / "sshd_config.d"
                / "00-vms-pilot-recovery.conf"
            )
            self.assertTrue(ssh_config.is_file())
            self.assertIn(
                "AuthenticationMethods publickey", ssh_config.read_text()
            )
            self.assertIn(
                "/etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf",
                contract["payload_sha256"],
            )
            self.assertNotIn(
                "/etc/ssh/sshd_config.d/90-vms-pilot-recovery.conf",
                contract["payload_sha256"],
            )
            service = (
                substage / "files" / "etc" / "systemd" / "system" / "vms-camera.service"
            ).read_text(encoding="utf-8")
            self.assertNotIn("network-online.target", service)
            recovery_service = (
                substage
                / "files"
                / "etc"
                / "systemd"
                / "system"
                / "vms-network-recovery.service"
            ).read_text(encoding="utf-8")
            recovery_timer = (
                substage
                / "files"
                / "etc"
                / "systemd"
                / "system"
                / "vms-network-recovery.timer"
            ).read_text(encoding="utf-8")
            self.assertIn("ReadWritePaths=/var/lib/vms", recovery_service)
            self.assertIn("OnUnitInactiveSec=30s", recovery_timer)
            install_script = (substage / "00-run.sh").read_text(encoding="utf-8")
            self.assertIn("python3 -c 'import picamera2'", install_script)
            self.assertIn("uart_2ndstage=1", install_script)
            self.assertIn("payload.sha256", install_script)
            self.assertIn(
                "/etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf",
                install_script,
            )
            self.assertNotIn(
                "systemctl enable ssh.service serial-getty@serial0.service",
                install_script,
            )
            self.assertIn(
                "systemctl disable serial-getty@serial0.service", install_script
            )

    def test_stage_owns_bounded_dispatch_cleanup_and_cloud_journal_routing(
        self,
    ) -> None:
        manifest = load_build_manifest(BUILD_MANIFEST)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stage2").mkdir()
            stage = root / "stage-vms-camera"
            assemble_pi_gen_stage(
                REPOSITORY,
                stage,
                manifest,
                source_revision="b" * 40,
            )
            files = stage / "00-vms-camera" / "files"

            provision = (files / "etc/systemd/system/vms-provision.service").read_text(
                encoding="utf-8"
            )
            dispatcher = (files / "usr/local/libexec/vms-provision").read_text(
                encoding="utf-8"
            )
            cleanup_unit = (
                files / "etc/systemd/system/vms-provision-cleanup.service"
            ).read_text(encoding="utf-8")
            cleanup = (files / "usr/local/libexec/vms-provision-cleanup").read_text(
                encoding="utf-8"
            )

            self.assertIn("Requires=cloud-init-network.service", provision)
            self.assertIn(
                "After=cloud-init-network.service NetworkManager.service",
                provision,
            )
            self.assertIn("JobTimeoutSec=15min", provision)
            self.assertIn("TimeoutStartSec=620s", provision)
            self.assertIn("StartLimitIntervalSec=0", provision)
            self.assertIn("Restart=on-failure", provision)
            self.assertIn("RestartSec=15s", provision)
            self.assertIn("RestartSteps=4", provision)
            self.assertIn("RestartMaxDelaySec=5min", provision)
            self.assertNotIn("StartLimitBurst", provision)
            self.assertNotIn("cloud-final.service", provision)
            self.assertNotIn("cloud-config.service", provision)
            self.assertNotIn("network-online.target", provision)
            self.assertIn("--value cloud-init-network.service", dispatcher)
            self.assertIn("= success", dispatcher)
            self.assertIn("systemctl start vms-first-boot.service", dispatcher)
            self.assertIn(
                'test "$(stat -c \'%U:%G:%a\' "$first_boot")" = root:root:700',
                dispatcher,
            )
            self.assertNotIn("sleep 1", dispatcher)

            self.assertIn("Requires=cloud-final.service", cleanup_unit)
            self.assertIn(
                "After=cloud-final.service vms-provision.service", cleanup_unit
            )
            self.assertIn("WantedBy=cloud-init.target", cleanup_unit)
            self.assertIn("Restart=on-failure", cleanup_unit)
            self.assertIn("StartLimitIntervalSec=0", cleanup_unit)
            self.assertIn("RestartSteps=4", cleanup_unit)
            self.assertIn("RestartMaxDelaySec=5min", cleanup_unit)
            self.assertNotIn("StartLimitBurst", cleanup_unit)
            self.assertIn("StandardOutput=journal", cleanup_unit)
            self.assertIn("--value cloud-final.service", cleanup)
            self.assertIn("/boot/firmware/user-data", cleanup)
            self.assertIn("/var/lib/cloud/instances", cleanup)
            self.assertIn("cloud-init*.log*", cleanup)
            self.assertIn("instance-data-sensitive.json", cleanup)
            self.assertLess(
                cleanup.index("Cloud-init file logs remain"),
                cleanup.index('mv -f "$temporary" "$marker"'),
            )
            marker_guard = cleanup.index('if test -e "$marker"')
            self.assertLess(marker_guard, cleanup.index("--value cloud-final.service"))
            self.assertLess(marker_guard, cleanup.index("/boot/firmware/user-data"))
            self.assertIn(
                'test "$(cat "$marker")" = \'{"schema_version":1,"state":"complete"}\'',
                cleanup,
            )
            self.assertIn("Cloud-init cleanup marker is invalid", cleanup)
            self.assertEqual(
                cleanup.count("systemctl disable vms-provision-cleanup.service"),
                2,
            )

            expected_stages = (
                "cloud-init-main.service",
                "cloud-init-local.service",
                "cloud-init-network.service",
                "cloud-config.service",
                "cloud-final.service",
            )
            for unit in expected_stages:
                with self.subTest(unit=unit):
                    drop_in = (
                        files
                        / "etc/systemd/system"
                        / f"{unit}.d"
                        / "99-vms-journal-only.conf"
                    ).read_text(encoding="utf-8")
                    self.assertEqual(
                        drop_in,
                        "[Service]\nStandardOutput=journal\nStandardError=journal\n",
                    )

            self.assertFalse((files / "usr/local/libexec/vms-first-boot").exists())
            self.assertFalse(
                (files / "etc/systemd/system/vms-first-boot.service").exists()
            )
            install_script = (stage / "00-vms-camera" / "00-run.sh").read_text(
                encoding="utf-8"
            )
            self.assertIn("systemd-analyze cat-config", install_script)
            self.assertIn("systemd-analyze verify", install_script)
            self.assertIn("vms-provision-cleanup.service", install_script)
            self.assertIn("loglevel=4", install_script)
            self.assertIn("systemd.show_status=1", install_script)
            self.assertNotIn("loglevel=7", install_script)
            self.assertNotIn("systemd.log_level=debug", install_script)

    def test_offline_runtime_contract_resolves_effective_unit_drop_ins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory) / "rootfs"
            self._write_runtime_contract(rootfs)

            verify_rootfs_runtime_contract(rootfs)

            override = (
                rootfs / "etc/systemd/system/cloud-final.service.d/zz-console.conf"
            )
            override.write_text(
                "[Service]\nStandardOutput=journal+console\n",
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaisesRegex(
                CameraImageBuildError, "cloud-final.service output is not journal-only"
            ):
                verify_rootfs_runtime_contract(rootfs)

    def test_offline_runtime_contract_rejects_generated_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory) / "rootfs"
            self._write_runtime_contract(rootfs)
            placeholder = rootfs / "usr/local/libexec/vms-first-boot"
            placeholder.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")

            with self.assertRaisesRegex(
                CameraImageBuildError, "per-device first-boot placeholder"
            ):
                verify_rootfs_runtime_contract(rootfs)

    def test_dispatcher_ready_marker_and_cleanup_marker_are_exact_short_circuits(
        self,
    ) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("POSIX shell is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            ready = temporary / "first-boot.json"
            dispatcher = image_build_module._VMS_PROVISION_SCRIPT.replace(
                "ready=/var/lib/vms/first-boot.json",
                f"ready={shlex.quote(ready.as_posix())}",
                1,
            )
            ready.write_text('{"schema_version":1,"state":"ready"}\n', encoding="utf-8")
            completed = subprocess.run(
                [shell], input=dispatcher.encode("utf-8"), capture_output=True
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            ready.write_text(
                '{"schema_version":1,"state":"failed"}\n', encoding="utf-8"
            )
            completed = subprocess.run(
                [shell], input=dispatcher.encode("utf-8"), capture_output=True
            )
            self.assertNotEqual(completed.returncode, 0)

            marker = temporary / "cloud-init-cleanup.json"
            cleanup = image_build_module._VMS_PROVISION_CLEANUP_SCRIPT.replace(
                "state_dir=/var/lib/vms",
                f"state_dir={shlex.quote(temporary.as_posix())}",
                1,
            )
            marker.write_text(
                '{"schema_version":1,"state":"complete"}\n', encoding="utf-8"
            )
            completed = subprocess.run(
                [shell], input=cleanup.encode("utf-8"), capture_output=True
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            marker.write_text(
                '{"schema_version":1,"state":"incomplete"}\n', encoding="utf-8"
            )
            completed = subprocess.run(
                [shell], input=cleanup.encode("utf-8"), capture_output=True
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(b"cleanup marker is invalid", completed.stderr)

    def test_offline_runtime_contract_rejects_retry_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory) / "rootfs"
            self._write_runtime_contract(rootfs)
            override = (
                rootfs / "etc/systemd/system/vms-provision.service.d/zz-no-retry.conf"
            )
            override.parent.mkdir(parents=True)
            override.write_text(
                "[Service]\nRestart=no\n", encoding="utf-8", newline="\n"
            )

            with self.assertRaisesRegex(CameraImageBuildError, "retry policy"):
                verify_rootfs_runtime_contract(rootfs)

    def test_build_host_check_fails_clearly_off_linux_or_without_tools(self) -> None:
        with self.assertRaisesRegex(CameraImageBuildError, "must be built on Linux"):
            check_build_host(platform="not-linux", which=lambda _command: None)
        with self.assertRaisesRegex(CameraImageBuildError, "docker, git"):
            check_build_host(platform="linux", which=lambda _command: None)
        available_without_e2fsck = {
            "docker": "/usr/bin/docker",
            "git": "/usr/bin/git",
            "debugfs": "/usr/sbin/debugfs",
        }
        with self.assertRaisesRegex(CameraImageBuildError, "e2fsck"):
            check_build_host(platform="linux", which=available_without_e2fsck.get)
        check_build_host(
            platform="linux",
            which={
                **available_without_e2fsck,
                "e2fsck": "/usr/sbin/e2fsck",
            }.get,
        )

    def test_docker_build_uses_pi_gen_default_deploy_directory(self) -> None:
        manifest = load_build_manifest(BUILD_MANIFEST)

        config = _pi_gen_config(manifest)

        self.assertNotIn("DEPLOY_DIR", config)
        self.assertNotIn("BASE_DIR", config)

    def test_pi_gen_compatibility_patch_preserves_signature_verification(self) -> None:
        patch = (PI_CAMERA_IMAGE / "pi-gen-gpgv.patch").read_text(encoding="utf-8")

        self.assertIn("gpg gpgv pigz", patch)
        self.assertNotIn("no-check-gpg", patch)

    def test_pi_gen_binfmt_patch_bootstraps_and_checks_arm(self) -> None:
        manifest = load_build_manifest(BUILD_MANIFEST)
        patch = PI_CAMERA_IMAGE / "pi-gen-binfmt.patch"
        patch_bytes = patch.read_bytes()
        pinned_block = (
            "".join(f"# pinned pi-gen line {line}\n" for line in range(1, 104))
            + r"""fi

# Check if binfmt_misc is required
binfmt_misc_required=1
case $(uname -m) in
  aarch64)
    binfmt_misc_required=0
    ;;
  arm*)
    binfmt_misc_required=0
    ;;
esac

# Check if qemu-arm and /proc/sys/fs/binfmt_misc are present
if [[ "${binfmt_misc_required}" == "1" ]]; then
  if ! qemu_arm=$(which qemu-arm) ; then
    echo "qemu-arm not found (please install qemu-user-binfmt)"
    exit 1
  fi
  if [ ! -f /proc/sys/fs/binfmt_misc/register ]; then
    echo "binfmt_misc required but not mounted, trying to mount it..."
    if ! mount binfmt_misc -t binfmt_misc /proc/sys/fs/binfmt_misc ; then
        echo "mounting binfmt_misc failed"
        exit 1
    fi
    echo "binfmt_misc mounted"
  fi
  if ! grep -q "^interpreter ${qemu_arm}" /proc/sys/fs/binfmt_misc/qemu-arm* ; then
    # Register qemu-arm for binfmt_misc
    reg="echo ':qemu-arm-rpi:M::"\
"\x7fELF\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x28\x00:"\
"\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:"\
"${qemu_arm}:F' > /proc/sys/fs/binfmt_misc/register"
    echo "Registering qemu-arm for binfmt_misc..."
    sudo bash -c "${reg}" 2>/dev/null || true
  fi
fi

trap 'echo "got CTRL+C... please wait 5s" && ${DOCKER} stop -t 5 ${DOCKER_CMDLINE_NAME}' SIGINT SIGTERM
time ${DOCKER} run \
  $DOCKER_CMDLINE_PRE \
  --name "${DOCKER_CMDLINE_NAME}" \
  --privileged \
  ${PIGEN_DOCKER_OPTS} \
  --volume "${CONFIG_FILE}":/config:ro \
  -e "GIT_HASH=${GIT_HASH}" \
  $DOCKER_CMDLINE_POST \
  pi-gen \
  bash -e -o pipefail -c "
    dpkg-reconfigure qemu-user-binfmt &&
    # binfmt_misc is sometimes not mounted with debian trixie image
    (mount binfmt_misc -t binfmt_misc /proc/sys/fs/binfmt_misc || true) &&
    cd /pi-gen; ./build.sh ${BUILD_OPTS} &&
    rsync -av work/*/build.log deploy/
  " &
  wait "$!"
"""
        )
        self.assertEqual(
            manifest.builder_commit,
            "314262cb286b8f33327a6f0cbabe14c625021ca0",
        )
        self.assertNotIn(b"\r", patch_bytes)
        self.assertTrue(patch_bytes.endswith(b"\n"))

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            target = checkout / "build-docker.sh"
            target.write_text(pinned_block, encoding="utf-8", newline="\n")
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "apply", "--check", os.fspath(patch)],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "apply", os.fspath(patch)],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            patched = target.read_text(encoding="utf-8")

        self.assertNotIn("which qemu-arm", patched)
        self.assertNotIn("binfmt_misc/qemu-arm*", patched)
        self.assertIn(
            "tonistiigi/binfmt@sha256:"
            "400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0",
            patched,
        )
        self.assertIn("--install arm", patched)
        self.assertGreaterEqual(patched.count("arch-test armhf"), 3)
        self.assertIn("cd /pi-gen && ./build.sh", patched)
        self.assertNotIn("cd /pi-gen; ./build.sh", patched)

    def test_pi_gen_export_patch_scrubs_and_checks_generic_first_boot_state(
        self,
    ) -> None:
        patch_path = PI_CAMERA_IMAGE / "pi-gen-first-boot-state.patch"
        patch_bytes = patch_path.read_bytes()
        patch = patch_bytes.decode("utf-8")

        self.assertNotIn(b"\r", patch_bytes)
        self.assertTrue(patch_bytes.endswith(b"\n"))
        self.assertIn('rm -rf "${ROOTFS_DIR}/var/lib/cloud"', patch)
        self.assertIn('find "${ROOTFS_DIR}/var/lib/cloud" -mindepth 1', patch)
        self.assertIn('rm -f "${ROOTFS_DIR}/var/lib/dbus/machine-id"', patch)
        self.assertIn("printf 'uninitialized\\n'", patch)
        self.assertIn("Machine-id was not reset before image export", patch)

    def test_pi_gen_export_patch_applies_to_pinned_finalise_context(self) -> None:
        manifest = load_build_manifest(BUILD_MANIFEST)
        patch = PI_CAMERA_IMAGE / "pi-gen-first-boot-state.patch"
        self.assertEqual(
            manifest.builder_commit,
            "314262cb286b8f33327a6f0cbabe14c625021ca0",
        )
        pinned_context = "".join(
            f"# pinned pi-gen line {line}\n" for line in range(1, 52)
        ) + (
            "\n"
            'rm -f "${ROOTFS_DIR}"/usr/share/icons/*/icon-theme.cache\n'
            "\n"
            'rm -f "${ROOTFS_DIR}/var/lib/dbus/machine-id"\n'
            "\n"
            'echo "uninitialized" > "${ROOTFS_DIR}/etc/machine-id"\n'
            "\n"
            'ln -nsf /proc/mounts "${ROOTFS_DIR}/etc/mtab"\n'
            "\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            target = checkout / "export-image" / "05-finalise" / "01-run.sh"
            target.parent.mkdir(parents=True)
            target.write_text(pinned_context, encoding="utf-8", newline="\n")
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "apply", "--check", os.fspath(patch)],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "apply", os.fspath(patch)],
                cwd=checkout,
                check=True,
                capture_output=True,
            )

            patched = target.read_text(encoding="utf-8")

        self.assertIn('rm -rf "${ROOTFS_DIR}/var/lib/cloud"', patched)
        self.assertIn("printf 'uninitialized\\n'", patched)
        self.assertNotIn('echo "uninitialized" >', patched)

    def test_pi_gen_patch_reconciliation_recovers_known_partial_state(self) -> None:
        first_patch = """diff --git a/first.txt b/first.txt
--- a/first.txt
+++ b/first.txt
@@ -1,3 +1,3 @@
 before
-first old
+first new
 after
"""
        second_patch = """diff --git a/second.txt b/second.txt
--- a/second.txt
+++ b/second.txt
@@ -1,3 +1,3 @@
 before
-second old
+second new
 after
"""
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "first.txt").write_text(
                "before\nfirst old\nafter\n", encoding="utf-8", newline="\n"
            )
            (checkout / "second.txt").write_text(
                "before\nsecond old\nafter\n", encoding="utf-8", newline="\n"
            )
            first_patch_path = checkout / "first.patch"
            second_patch_path = checkout / "second.patch"
            first_patch_path.write_text(first_patch, encoding="utf-8", newline="\n")
            second_patch_path.write_text(second_patch, encoding="utf-8", newline="\n")
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=checkout,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "apply", os.fspath(first_patch_path)],
                cwd=checkout,
                check=True,
                capture_output=True,
            )

            image_build_module._apply_pi_gen_patches(
                checkout, (first_patch_path, second_patch_path)
            )

            self.assertEqual(
                (checkout / "first.txt").read_text(encoding="utf-8"),
                "before\nfirst new\nafter\n",
            )
            self.assertEqual(
                (checkout / "second.txt").read_text(encoding="utf-8"),
                "before\nsecond new\nafter\n",
            )

    def test_offline_rootfs_contract_accepts_only_clean_first_boot_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory)
            self._write_clean_first_boot_state(rootfs)

            verify_rootfs_first_boot_state(rootfs)

    def test_offline_rootfs_contract_rejects_every_cloud_init_cache_shape(self) -> None:
        dirty_paths = (
            "var/lib/cloud/instance/sem/config_users_groups",
            "var/lib/cloud/instances/rpios-image/sem/config_users_groups",
            "var/lib/cloud/seed/nocloud/meta-data",
            "var/lib/cloud/seed/nocloud-net/user-data",
            "var/lib/cloud/data/instance-id",
        )
        for dirty_path in dirty_paths:
            with (
                self.subTest(path=dirty_path),
                tempfile.TemporaryDirectory() as directory,
            ):
                rootfs = Path(directory)
                self._write_clean_first_boot_state(rootfs)
                path = rootfs / dirty_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("build-state\n", encoding="utf-8")

                with self.assertRaisesRegex(
                    CameraImageBuildError, "build-time cloud-init state"
                ):
                    verify_rootfs_first_boot_state(rootfs)

    def test_offline_rootfs_contract_rejects_non_reset_machine_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory)
            self._write_clean_first_boot_state(rootfs)
            (rootfs / "etc" / "machine-id").write_text(
                "0123456789abcdef0123456789abcdef\n", encoding="ascii"
            )

            with self.assertRaisesRegex(CameraImageBuildError, "was not reset"):
                verify_rootfs_first_boot_state(rootfs)

        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory)
            self._write_clean_first_boot_state(rootfs)
            dbus_machine_id = rootfs / "var" / "lib" / "dbus" / "machine-id"
            dbus_machine_id.parent.mkdir(parents=True, exist_ok=True)
            dbus_machine_id.write_text("build-machine\n", encoding="ascii")

            with self.assertRaisesRegex(CameraImageBuildError, "D-Bus machine-id"):
                verify_rootfs_first_boot_state(rootfs)

    def test_compressed_image_inspection_records_raw_layout_and_hash(self) -> None:
        raw = bytearray(4096 * 512)
        raw[510:512] = b"\x55\xaa"
        self._write_partition(raw, index=0, kind=0x0C, start=8, sectors=256)
        self._write_partition(raw, index=1, kind=0x83, start=264, sectors=1024)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "camera.img.xz"
            with lzma.open(image, "wb") as stream:
                stream.write(raw)

            inspection = inspect_xz_image(image)

        self.assertEqual(inspection.bytes, len(raw))
        self.assertEqual(len(inspection.sha256), 64)
        self.assertEqual(inspection.partitions[0].start_sector, 8)
        self.assertEqual(inspection.partitions[1].type, "0x83")
        self.assertEqual(inspection.partitions[1].to_dict()["byte_offset"], 264 * 512)

    def test_compressed_image_inspection_rejects_partition_outside_image(self) -> None:
        raw = bytearray(1024 * 512)
        raw[510:512] = b"\x55\xaa"
        self._write_partition(raw, index=0, kind=0x0C, start=8, sectors=256)
        self._write_partition(raw, index=1, kind=0x83, start=264, sectors=2048)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "camera.img.xz"
            with lzma.open(image, "wb") as stream:
                stream.write(raw)

            with self.assertRaisesRegex(CameraImageBuildError, "exceeds"):
                inspect_xz_image(image)

    def test_exported_rootfs_verification_extracts_exact_partition_and_cleans_temp(
        self,
    ) -> None:
        raw = bytes(range(256)) * 16
        partition = RawImagePartition(
            index=2,
            type="0x83",
            start_sector=2,
            sector_count=4,
        )
        inspection = RawImageInspection(
            bytes=len(raw),
            sha256="a" * 64,
            partitions=(partition,),
        )
        extracted_roots: list[Path] = []

        def run_tool(
            arguments: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "/usr/sbin/e2fsck":
                self.assertEqual(
                    Path(arguments[-1]).read_bytes(), raw[1024 : 1024 + 2048]
                )
                return subprocess.CompletedProcess(arguments, 0, "", "")
            self.assertEqual(arguments[0], "/usr/sbin/debugfs")
            extracted_rootfs = Path(shlex.split(arguments[2])[2])
            diagnostics = (
                "debugfs 1.47.0\n"
                "rdump: Operation not permitted while changing ownership of "
                f"{extracted_rootfs}/\n"
                "dump_file: Operation not permitted while changing ownership of "
                f"{extracted_rootfs}//usr/local/libexec/vms-provision\n"
            )
            return subprocess.CompletedProcess(arguments, 0, "", diagnostics)

        def record_rootfs(path: Path) -> None:
            self.assertTrue(path.is_dir())
            extracted_roots.append(path)

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "camera.img.xz"
            with lzma.open(image, "wb") as stream:
                stream.write(raw)
            with (
                mock.patch.object(
                    image_build_module.shutil,
                    "which",
                    side_effect=lambda command: f"/usr/sbin/{command}",
                ),
                mock.patch.object(
                    image_build_module.subprocess, "run", side_effect=run_tool
                ) as runner,
                mock.patch.object(
                    image_build_module,
                    "verify_rootfs_first_boot_state",
                    side_effect=record_rootfs,
                ) as first_boot_verifier,
                mock.patch.object(
                    image_build_module,
                    "verify_rootfs_runtime_contract",
                    side_effect=record_rootfs,
                ) as runtime_verifier,
            ):
                verify_exported_image_rootfs(image, inspection)

        self.assertEqual(runner.call_count, 2)
        first_boot_verifier.assert_called_once()
        runtime_verifier.assert_called_once()
        self.assertEqual(extracted_roots[0], extracted_roots[1])
        self.assertFalse(extracted_roots[0].exists())

    def test_exported_rootfs_verification_rejects_unexpected_debugfs_stderr(
        self,
    ) -> None:
        raw = bytes(2048)
        partition = RawImagePartition(
            index=1,
            type="0x83",
            start_sector=1,
            sector_count=2,
        )
        inspection = RawImageInspection(
            bytes=len(raw),
            sha256="a" * 64,
            partitions=(partition,),
        )

        def run_tool(
            arguments: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "/usr/sbin/e2fsck":
                return subprocess.CompletedProcess(arguments, 0, "", "")
            return subprocess.CompletedProcess(
                arguments,
                0,
                "",
                "debugfs 1.47.0\nrdump: Operation not permitted while reading inode\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "camera.img.xz"
            with lzma.open(image, "wb") as stream:
                stream.write(raw)
            with (
                mock.patch.object(
                    image_build_module.shutil,
                    "which",
                    side_effect=lambda command: f"/usr/sbin/{command}",
                ),
                mock.patch.object(
                    image_build_module.subprocess, "run", side_effect=run_tool
                ),
                mock.patch.object(
                    image_build_module, "verify_rootfs_first_boot_state"
                ) as first_boot_verifier,
                mock.patch.object(
                    image_build_module, "verify_rootfs_runtime_contract"
                ) as runtime_verifier,
            ):
                with self.assertRaisesRegex(
                    CameraImageBuildError, "debugfs reported an error"
                ):
                    verify_exported_image_rootfs(image, inspection)

        first_boot_verifier.assert_not_called()
        runtime_verifier.assert_not_called()

    @unittest.skipUnless(
        os.name == "posix"
        and shutil.which("mke2fs") is not None
        and shutil.which("debugfs") is not None
        and shutil.which("e2fsck") is not None,
        "Linux e2fsprogs integration tools are unavailable",
    )
    def test_exported_rootfs_verification_reads_a_real_ext4_partition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source_rootfs = temporary / "source-rootfs"
            self._write_runtime_contract(source_rootfs)
            self._write_clean_first_boot_state(source_rootfs)
            ext4 = temporary / "root.ext4"
            subprocess.run(
                [
                    "mke2fs",
                    "-q",
                    "-F",
                    "-t",
                    "ext4",
                    "-b",
                    "4096",
                    "-d",
                    os.fspath(source_rootfs),
                    os.fspath(ext4),
                    "8192",
                ],
                check=True,
                capture_output=True,
            )
            for field in ("uid", "gid"):
                subprocess.run(
                    [
                        "debugfs",
                        "-w",
                        "-R",
                        f"set_inode_field /usr/local/libexec/vms-provision {field} 0",
                        os.fspath(ext4),
                    ],
                    check=True,
                    capture_output=True,
                )
            root_start = 2048
            root_sectors = ext4.stat().st_size // 512
            raw = temporary / "camera.img"
            with raw.open("wb") as stream:
                stream.truncate((root_start + root_sectors) * 512)
                sector = bytearray(512)
                sector[510:512] = b"\x55\xaa"
                self._write_partition(sector, index=0, kind=0x0C, start=8, sectors=1024)
                self._write_partition(
                    sector,
                    index=1,
                    kind=0x83,
                    start=root_start,
                    sectors=root_sectors,
                )
                stream.seek(0)
                stream.write(sector)
                stream.seek(root_start * 512)
                with ext4.open("rb") as root_partition:
                    shutil.copyfileobj(root_partition, stream)
            image = temporary / "camera.img.xz"
            with raw.open("rb") as source, lzma.open(image, "wb") as compressed:
                shutil.copyfileobj(source, compressed)

            inspection = inspect_xz_image(image)
            real_run = subprocess.run
            debugfs_diagnostics: list[str] = []

            def record_debugfs(
                arguments: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                completed = real_run(arguments, **kwargs)
                if Path(arguments[0]).name == "debugfs" and arguments[1] == "-R":
                    debugfs_diagnostics.extend(completed.stderr.splitlines())
                return completed

            with mock.patch.object(
                image_build_module.subprocess, "run", side_effect=record_debugfs
            ):
                verify_exported_image_rootfs(image, inspection)

            self.assertTrue(
                any(
                    line.startswith(
                        "dump_file: Operation not permitted while changing ownership "
                    )
                    and line.endswith("/usr/local/libexec/vms-provision")
                    for line in debugfs_diagnostics
                ),
                debugfs_diagnostics,
            )

    def test_build_fails_before_publishing_when_exported_rootfs_is_invalid(
        self,
    ) -> None:
        inspection = RawImageInspection(
            bytes=4096,
            sha256="d" * 64,
            partitions=(
                RawImagePartition(1, "0x0c", 1, 1),
                RawImagePartition(2, "0x83", 2, 2),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            work = temporary / "work"
            output = temporary / "output"
            pi_gen = work / "pi-gen"
            pi_gen.mkdir(parents=True)

            def run_build(
                arguments: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if arguments[0] == "./build-docker.sh":
                    deploy = pi_gen / "deploy"
                    deploy.mkdir()
                    (deploy / "built.img.xz").write_bytes(b"copied artifact")
                return subprocess.CompletedProcess(arguments, 0, "", "")

            def reject_rootfs(path: Path, actual: RawImageInspection) -> None:
                self.assertEqual(actual, inspection)
                self.assertTrue(path.is_file())
                self.assertTrue(path.parent.name.startswith(".vms-camera-verify-"))
                self.assertFalse(tuple(output.glob("vms-camera-*")))
                raise CameraImageBuildError("exported rootfs contract failed")

            with (
                mock.patch.object(
                    image_build_module,
                    "_require_clean_versioned_source",
                    return_value="e" * 40,
                ),
                mock.patch.object(image_build_module, "check_build_host"),
                mock.patch.object(
                    image_build_module, "_prepare_pi_gen", return_value=pi_gen
                ),
                mock.patch.object(image_build_module, "assemble_pi_gen_stage"),
                mock.patch.object(
                    image_build_module.subprocess, "run", side_effect=run_build
                ),
                mock.patch.object(
                    image_build_module, "inspect_xz_image", return_value=inspection
                ),
                mock.patch.object(
                    image_build_module,
                    "verify_exported_image_rootfs",
                    side_effect=reject_rootfs,
                ) as verifier,
            ):
                with self.assertRaisesRegex(
                    CameraImageBuildError, "exported rootfs contract failed"
                ):
                    build_camera_image(
                        REPOSITORY,
                        BUILD_MANIFEST,
                        work,
                        output,
                    )

            verifier.assert_called_once()
            self.assertFalse(tuple(output.glob("vms-camera-*")))
            self.assertFalse(tuple(output.glob(".vms-camera-verify-*")))

    @staticmethod
    def _write_partition(
        raw: bytearray, *, index: int, kind: int, start: int, sectors: int
    ) -> None:
        offset = 446 + index * 16
        raw[offset + 4] = kind
        raw[offset + 8 : offset + 12] = start.to_bytes(4, "little")
        raw[offset + 12 : offset + 16] = sectors.to_bytes(4, "little")

    @staticmethod
    def _write_clean_first_boot_state(rootfs: Path) -> None:
        (rootfs / "var" / "lib" / "cloud").mkdir(parents=True)
        machine_id = rootfs / "etc" / "machine-id"
        machine_id.parent.mkdir(parents=True, exist_ok=True)
        machine_id.write_bytes(b"uninitialized\n")

    @staticmethod
    def _write_runtime_contract(rootfs: Path) -> None:
        stage_root = rootfs.parent / "pi-gen"
        (stage_root / "stage2").mkdir(parents=True)
        stage = stage_root / "stage-vms-camera"
        assemble_pi_gen_stage(
            REPOSITORY,
            stage,
            load_build_manifest(BUILD_MANIFEST),
            source_revision="c" * 40,
        )
        shutil.copytree(stage / "00-vms-camera" / "files", rootfs)

        vendor = rootfs / "usr/lib/systemd/system"
        vendor.mkdir(parents=True)
        for unit in (
            "cloud-init-main.service",
            "cloud-init-local.service",
            "cloud-init-network.service",
            "cloud-config.service",
            "cloud-final.service",
        ):
            (vendor / unit).write_text(
                "[Unit]\nDescription=Test cloud-init stage\n\n"
                "[Service]\nType=oneshot\nExecStart=/bin/true\n"
                "StandardOutput=journal+console\nStandardError=inherit\n",
                encoding="utf-8",
                newline="\n",
            )

        links = (
            ("multi-user.target", "vms-provision.service"),
            ("cloud-init.target", "vms-provision-cleanup.service"),
        )
        for target, unit in links:
            wants = rootfs / "etc/systemd/system" / f"{target}.wants"
            wants.mkdir(parents=True, exist_ok=True)
            os.symlink(f"../{unit}", wants / unit)


if __name__ == "__main__":
    unittest.main()
