import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from video_monitoring_system.device.pilot import (
    PILOT_DIAGNOSTICS_SCRIPT,
    PILOT_USB_RECOVERY_SCRIPT,
    PILOT_USB_RECOVERY_SERVICE,
    PILOT_USB_RECOVERY_UNIT,
)


class PilotUsbRecoveryContractTests(unittest.TestCase):
    def test_script_has_valid_posix_shell_syntax(self) -> None:
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("a POSIX shell is not installed")

        result = subprocess.run(
            [shell, "-n"],
            input=PILOT_USB_RECOVERY_SCRIPT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unit_retries_bounded_script_failures_without_a_path_condition(
        self,
    ) -> None:
        self.assertEqual(PILOT_USB_RECOVERY_SERVICE, PILOT_USB_RECOVERY_UNIT)
        self.assertIn("Restart=on-failure", PILOT_USB_RECOVERY_UNIT)
        self.assertIn("RestartSec=15s", PILOT_USB_RECOVERY_UNIT)
        self.assertIn("TimeoutStartSec=75s", PILOT_USB_RECOVERY_UNIT)
        self.assertIn("StartLimitIntervalSec=0", PILOT_USB_RECOVERY_UNIT)
        self.assertIn("Before=serial-getty@ttyGS0.service", PILOT_USB_RECOVERY_UNIT)
        self.assertNotIn("ConditionPathExists=/sys/class/udc", PILOT_USB_RECOVERY_UNIT)

    def test_binding_is_after_every_configuration_write(self) -> None:
        bind = 'printf \'%s\\n\' "$udc" > "$gadget/UDC"'

        self.assertEqual(PILOT_USB_RECOVERY_SCRIPT.count(bind), 1)
        self.assertLess(
            PILOT_USB_RECOVERY_SCRIPT.index(
                '[ -L "$function_link" ] || ln -s "$function" "$function_link"'
            ),
            PILOT_USB_RECOVERY_SCRIPT.index(bind),
        )
        self.assertLess(
            PILOT_USB_RECOVERY_SCRIPT.index('if owner=$(foreign_owner "$udc")'),
            PILOT_USB_RECOVERY_SCRIPT.index(bind),
        )
        self.assertGreater(
            PILOT_USB_RECOVERY_SCRIPT.index('wait_until_ready "$udc"',
                                            PILOT_USB_RECOVERY_SCRIPT.index(bind)),
            PILOT_USB_RECOVERY_SCRIPT.index(bind),
        )

    def test_diagnostics_include_baked_provisioning_units(self) -> None:
        self.assertIn("-u vms-provision.service", PILOT_DIAGNOSTICS_SCRIPT)
        self.assertIn("-u vms-provision-cleanup.service", PILOT_DIAGNOSTICS_SCRIPT)


class PilotUsbRecoveryBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.shell = shutil.which("sh")
        if self.shell is None:
            self.skipTest("a POSIX shell is not installed")
        if os.name == "nt" and not any(
            marker in self.shell.lower() for marker in ("msys", "git")
        ):
            self.skipTest("the available shell cannot access the test filesystem")

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sys_root = self.root / "sys"
        self.dev_root = self.root / "dev"
        self.udc_root = self.sys_root / "class" / "udc"
        self.gadget_root = self.sys_root / "kernel" / "config" / "usb_gadget"
        self.own_gadget = self.gadget_root / "vms-pilot-recovery"
        self.script = self.root / "vms-usb-recovery"
        self.machine_id = self.root / "machine-id"
        for directory in (
            self.dev_root,
            self.udc_root,
            self.gadget_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.machine_id.write_text(
            "0123456789abcdef0123456789abcdef\n", encoding="ascii"
        )
        self._write_executable(self.script, self._testable_script())

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def test_missing_udc_fails_after_bounded_wait_without_claiming_success(
        self,
    ) -> None:
        result = self.run_recovery()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "No USB device controller appeared after 4 attempts", result.stderr
        )
        self.assertFalse((self.root / "systemctl.log").exists())
        self.assertLessEqual(self.sleep_count, 30)

    def test_delayed_udc_is_bound_only_after_configuration_and_tty_is_verified(
        self,
    ) -> None:
        result = self.run_recovery(delay_udc_at=3, create_tty=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.own_gadget / "UDC").read_text().strip(), "test-udc")
        self.assertTrue((self.dev_root / "ttyGS0").exists())
        self.assertTrue((self.own_gadget / "functions" / "acm.usb0").is_dir())
        function_link = self.own_gadget / "configs" / "c.1" / "acm.usb0"
        self.assertTrue(function_link.exists())
        if os.name == "posix":
            self.assertTrue(function_link.is_symlink())
        self.assertIn(
            "--no-block start serial-getty@ttyGS0.service",
            (self.root / "systemctl.log").read_text(encoding="utf-8"),
        )
        self.assertIn("ready on UDC test-udc", result.stdout)

    def test_healthy_rerun_is_idempotent_and_does_not_rebind(self) -> None:
        first = self.run_recovery(delay_udc_at=1, create_tty=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        sleeps_after_first = self.sleep_count

        second = self.run_recovery(create_tty=False)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.sleep_count, sleeps_after_first)
        self.assertEqual((self.own_gadget / "UDC").read_text().strip(), "test-udc")
        calls = (self.root / "systemctl.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls.count("--no-block start serial-getty@ttyGS0.service"), 2)

    def test_failed_tty_verification_unbinds_and_a_retry_reuses_partial_setup(
        self,
    ) -> None:
        self.add_udc()
        failed = self.run_recovery(create_tty=False)

        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("did not create", failed.stderr)
        self.assertEqual((self.own_gadget / "UDC").read_text().strip(), "")
        self.assertFalse((self.root / "systemctl.log").exists())

        retried = self.run_recovery(create_tty=True)

        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual((self.own_gadget / "UDC").read_text().strip(), "test-udc")

    def test_foreign_gadget_owner_is_reported_and_never_modified(self) -> None:
        self.add_udc()
        foreign_udc = self.gadget_root / "foreign-gadget" / "UDC"
        foreign_udc.parent.mkdir()
        foreign_udc.write_text("test-udc\n", encoding="ascii")

        result = self.run_recovery(create_tty=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign-gadget", result.stderr)
        self.assertIn("No USB device controller is free", result.stderr)
        self.assertNotIn("No USB device controller appeared", result.stderr)
        self.assertEqual(self.sleep_count, 0)
        self.assertEqual(foreign_udc.read_text(encoding="ascii"), "test-udc\n")
        self.assertFalse(self.own_gadget.exists())
        self.assertFalse((self.root / "systemctl.log").exists())

    @property
    def sleep_count(self) -> int:
        path = self.root / "sleep-count"
        return int(path.read_text(encoding="ascii")) if path.exists() else 0

    def add_udc(self) -> None:
        (self.udc_root / "test-udc").mkdir(exist_ok=True)

    def run_recovery(
        self,
        *,
        delay_udc_at: int | None = None,
        create_tty: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "MSYS": "winsymlinks:nativestrict",
                "PATH": "/usr/bin:/bin",
                "TEST_ROOT": self._shell_path(self.root),
                "TEST_DELAY_UDC_AT": "" if delay_udc_at is None else str(delay_udc_at),
                "TEST_CREATE_TTY": "1" if create_tty else "0",
                "VMS_USB_RECOVERY_SYS_ROOT": self._shell_path(self.sys_root),
                "VMS_USB_RECOVERY_DEV_ROOT": self._shell_path(self.dev_root),
                "VMS_USB_RECOVERY_MACHINE_ID_FILE": self._shell_path(self.machine_id),
                "VMS_USB_RECOVERY_TEST_ALLOW_NONCHAR_TTY": "1",
                "VMS_USB_RECOVERY_TEST_UDC_WAIT_ATTEMPTS": "4",
                "VMS_USB_RECOVERY_TEST_TTY_WAIT_ATTEMPTS": "3",
            }
        )
        return subprocess.run(
            [self.shell, self._shell_path(self.script)],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            env=environment,
        )

    @staticmethod
    def _testable_script() -> str:
        overrides = r"""
modprobe() { return 0; }
mountpoint() { return 0; }
mount() { return 1; }
systemctl() { printf '%s\n' "$*" >> "$TEST_ROOT/systemctl.log"; }
sleep() {
count_file="$TEST_ROOT/sleep-count"
count=0
if [ -r "$count_file" ]; then IFS= read -r count < "$count_file" || true; fi
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"
if [ -n "${TEST_DELAY_UDC_AT:-}" ] && [ "$count" -eq "$TEST_DELAY_UDC_AT" ]; then
    mkdir -p "$VMS_USB_RECOVERY_SYS_ROOT/class/udc/test-udc"
fi
if [ "${TEST_CREATE_TTY:-0}" -eq 1 ]; then
    gadget="$VMS_USB_RECOVERY_SYS_ROOT/kernel/config/usb_gadget/vms-pilot-recovery"
    udc_file="$gadget/UDC"
    if [ -s "$udc_file" ]; then
        : > "$VMS_USB_RECOVERY_DEV_ROOT/ttyGS0"
    fi
fi
}
"""
        return PILOT_USB_RECOVERY_SCRIPT.replace(
            "set -eu\n", f"set -eu\n{overrides}", 1
        )

    @staticmethod
    def _write_executable(path: Path, contents: str) -> None:
        path.write_text(contents, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    @staticmethod
    def _shell_path(path: Path) -> str:
        value = path.resolve().as_posix()
        if os.name == "nt" and len(value) >= 3 and value[1:3] == ":/":
            return f"/{value[0].lower()}{value[2:]}"
        return value


if __name__ == "__main__":
    unittest.main()
