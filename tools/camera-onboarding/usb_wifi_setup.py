#!/usr/bin/env python3
"""Local UI for pilot camera Wi-Fi setup over USB serial or SSH."""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

try:
    import serial
except ImportError:  # SSH-only use does not need pyserial.
    serial = None


SHELL_PROMPT_RE = re.compile(rb"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[^\r\n]*[$#]\s*$")
LOGIN_PROMPT_RE = re.compile(rb"[A-Za-z0-9_.-]+\s+login:\s*$", re.IGNORECASE)
PASSWORD_PROMPT_RE = re.compile(rb"password[^:\r\n]*:\s*$", re.IGNORECASE)
SUDO_MARKER = b"__VMS_CAMERA_SUDO__"
RESULT_RE = re.compile(rb"__VMS_RESULT=(\d+)__")
WIFI_SECRET_MARKER = b"__VMS_WIFI_SECRET__"


def network_manager_script(ssid: str, network_device: str) -> str:
    return (
        "set -u; "
        f"target_ssid={shlex.quote(ssid)}; "
        "camera_number=$(python3 -c 'import json,sys; n=json.load(open(sys.argv[1])).get(\"camera_number\"); "
        "assert isinstance(n,int) and not isinstance(n,bool) and 1 <= n <= 999; print(f\"{n:03d}\")' "
        "/etc/vms/device.json) || exit $?; "
        "home_profile=\"vms-home-$camera_number\"; "
        "legacy_profile=\"vms-camera-$camera_number\"; "
        "profile_name=''; secret_file=''; keep_profile=0; wifi_password=''; "
        "cleanup() { "
        "[ -z \"$secret_file\" ] || rm -f -- \"$secret_file\"; "
        "if [ \"$keep_profile\" -ne 1 ] && [ -n \"$profile_name\" ]; then "
        "nmcli connection delete id \"$profile_name\" >/dev/null 2>&1 || true; "
        "fi; unset wifi_password; }; "
        "trap cleanup EXIT HUP INT TERM; "
        "visible_ssids=$(LC_ALL=C nmcli --colors no -t -e no -f SSID "
        f"device wifi list ifname {shlex.quote(network_device)}) || exit $?; "
        "if ! printf '%s\\n' \"$visible_ssids\" | grep -Fqx \"$target_ssid\"; then "
        "printf '__VMS_NO_NETWORK__\\n'; exit 78; fi; "
        "profile_name=\"vms-home-setup-$(cat /proc/sys/kernel/random/uuid)\" || exit $?; "
        "nmcli connection add type wifi "
        f"ifname {shlex.quote(network_device)} "
        "con-name \"$profile_name\" ssid \"$target_ssid\" "
        "wifi-sec.key-mgmt wpa-psk connection.autoconnect yes "
        "connection.autoconnect-priority 100 >/dev/null || exit $?; "
        "secret_file=$(mktemp /run/vms-wifi-pass.XXXXXX) || exit $?; "
        "chmod 600 \"$secret_file\" || exit $?; "
        "printf '__VMS_WIFI_SECRET__\\n'; "
        "IFS= read -r wifi_password || exit 70; "
        "printf '802-11-wireless-security.psk:%s\\n' \"$wifi_password\" "
        "> \"$secret_file\" || exit $?; unset wifi_password; "
        "if nmcli --wait 75 connection up id \"$profile_name\" "
        f"ifname {shlex.quote(network_device)} "
        "passwd-file \"$secret_file\"; then rc=0; else rc=$?; exit \"$rc\"; fi; "
        "rm -f -- \"$secret_file\"; secret_file=''; keep_profile=1; "
        "active_uuids=$(nmcli -g UUID connection show --active) || exit $?; "
        "all_uuids=$(nmcli -g UUID connection show) || exit $?; "
        "for uuid in $all_uuids; do "
        "connection_id=$(nmcli -g connection.id connection show \"$uuid\" 2>/dev/null) || continue; "
        "if [ \"$connection_id\" = \"$home_profile\" ] && "
        "! printf '%s\\n' \"$active_uuids\" | grep -Fqx \"$uuid\"; then "
        "nmcli connection delete uuid \"$uuid\" >/dev/null || exit $?; "
        "fi; done; "
        "nmcli connection modify id \"$profile_name\" connection.id \"$home_profile\" "
        "connection.autoconnect yes connection.autoconnect-priority 100 || exit $?; "
        "profile_name=\"$home_profile\"; "
        "active_uuids=$(nmcli -g UUID connection show --active) || exit $?; "
        "all_uuids=$(nmcli -g UUID connection show) || exit $?; "
        "for uuid in $all_uuids; do "
        "connection_id=$(nmcli -g connection.id connection show \"$uuid\" 2>/dev/null) || continue; "
        "profile_ssid=$(nmcli -g 802-11-wireless.ssid connection show \"$uuid\" 2>/dev/null) || true; "
        "if ! printf '%s\\n' \"$active_uuids\" | grep -Fqx \"$uuid\" && "
        "{ [ \"$connection_id\" = \"$legacy_profile\" ] || [ \"$profile_ssid\" = \"$target_ssid\" ]; }; then "
        "nmcli connection delete uuid \"$uuid\" >/dev/null || exit $?; "
        "fi; done; "
        f"wpa_cli -i {shlex.quote(network_device)} status "
        "| grep -E '^(wpa_state|ip_address)='; "
        "find /etc/NetworkManager/system-connections -maxdepth 1 "
        "-type f -name '*.nmconnection' "
        "-printf '__PROFILE_MODE=%m__\\n' 2>/dev/null; exit 0"
    )


@dataclass(frozen=True)
class SetupOutcome:
    success: bool
    message: str


class SerialNetworkManagerAdapter:
    """Send a Wi-Fi profile through an already-connected USB serial console."""

    def __init__(
        self,
        serial_port: pathlib.Path,
        recovery_state: pathlib.Path,
        recovery_field: str,
        camera_number: int | None,
        console_user: str,
        network_device: str,
    ) -> None:
        self.serial_port = serial_port
        self.recovery_state = recovery_state
        self.recovery_field = recovery_field
        self.camera_number = camera_number
        self.console_user = console_user
        self.network_device = network_device

    def connect(self, ssid: str, wifi_password: str) -> SetupOutcome:
        recovery_password = ""
        try:
            state = json.loads(self.recovery_state.read_text())
            if self.camera_number is None:
                recovery_password = state[self.recovery_field]
            else:
                cameras = state.get("identity", {}).get("cameras", [])
                matches = [
                    camera
                    for camera in cameras
                    if camera.get("camera_number") == self.camera_number
                ]
                if len(matches) != 1:
                    return SetupOutcome(
                        False, "The selected camera recovery identity is unavailable."
                    )
                recovery_password = matches[0][self.recovery_field]
            if not isinstance(recovery_password, str) or not recovery_password:
                return SetupOutcome(False, "The camera recovery login is unavailable.")
            if not self.serial_port.exists():
                return SetupOutcome(False, "Camera USB console is not connected.")

            with serial.Serial(
                str(self.serial_port),
                115200,
                timeout=0.15,
                write_timeout=5,
            ) as port:
                try:
                    shell_ready = self._ensure_shell(port, recovery_password)
                except serial.SerialTimeoutException:
                    return SetupOutcome(
                        False,
                        "The camera USB console did not accept input. "
                        "The Wi-Fi password was not sent.",
                    )
                if not shell_ready:
                    return SetupOutcome(
                        False,
                        "Camera console login was not confirmed. No Wi-Fi profile was sent.",
                    )
                return self._connect_from_shell(
                    port, ssid, wifi_password, recovery_password
                )
        except Exception:
            return SetupOutcome(
                False, "USB setup could not finish. Leave the camera connected."
            )
        finally:
            recovery_password = ""
            wifi_password = ""

    def _connect_from_shell(
        self,
        port: serial.Serial,
        ssid: str,
        wifi_password: str,
        recovery_password: str,
    ) -> SetupOutcome:
        echo_disabled = False
        shell_ready = False
        try:
            # Disable echo separately so typed sudo prompt text cannot fool the
            # response detector or expose either password in terminal output.
            port.write(b"stty -echo\n")
            echo_disabled = True
            if not SHELL_PROMPT_RE.search(self._read_prompt(port, 3)):
                return SetupOutcome(
                    False,
                    "Camera console echo could not be disabled safely. "
                    "No Wi-Fi password was sent.",
                )

            inner = network_manager_script(ssid, self.network_device)
            command = (
                "sudo -k; sudo -S -p '__VMS_CAMERA_SUDO__' sh -c "
                f"{shlex.quote(inner)}; rc=$?; "
                "printf '__VMS_RESULT=%s__\\n' \"$rc\"\n"
            )
            try:
                port.write(command.encode("utf-8"))
            except serial.SerialTimeoutException:
                return SetupOutcome(
                    False,
                    "The camera USB console did not accept setup commands. "
                    "The Wi-Fi password was not sent.",
                )
            received = bytearray()
            sent_sudo = False
            sent_wifi_password = False
            deadline = time.monotonic() + 100

            while time.monotonic() < deadline:
                chunk = port.read(2048)
                if not chunk:
                    continue
                received.extend(chunk)

                if SUDO_MARKER in received and not sent_sudo:
                    port.write(recovery_password.encode("utf-8") + b"\n")
                    sent_sudo = True

                if WIFI_SECRET_MARKER in received and not sent_wifi_password:
                    sent_wifi_password = True
                    try:
                        port.write(wifi_password.encode("utf-8") + b"\n")
                    except serial.SerialTimeoutException:
                        return SetupOutcome(
                            False,
                            "The USB link failed while sending the Wi-Fi password. "
                            "The result is uncertain; check the camera before retrying.",
                        )
                    wifi_password = ""

                result = RESULT_RE.search(received)
                if result:
                    shell_ready = True
                    rc = int(result.group(1))
                    current = received.decode("utf-8", "replace")
                    connected = "wpa_state=COMPLETED" in current
                    has_address = bool(
                        re.search(r"(?m)^ip_address=[0-9.]+\s*$", current)
                    )
                    modes = re.findall(r"__PROFILE_MODE=([0-7]{3,4})__", current)
                    protected = bool(modes) and all(mode == "600" for mode in modes)

                    if rc == 0 and connected and has_address and protected:
                        return SetupOutcome(
                            True,
                            "Connected. The camera has a DHCP address and its "
                            "NetworkManager profiles are mode 600. USB remains "
                            "attached for recovery.",
                        )
                    if rc == 0 and connected:
                        return SetupOutcome(
                            True,
                            "Connected to Wi-Fi. USB remains attached. "
                            "The DHCP or profile-permission check needs follow-up.",
                        )
                    if b"__VMS_NO_NETWORK__" in received:
                        return SetupOutcome(
                            False,
                            "The camera cannot see that Wi-Fi name on 2.4 GHz. "
                            "Check the spelling and try again.",
                        )
                    if "no network with ssid" in current.lower():
                        return SetupOutcome(
                            False,
                            "The camera could not find that network. Choose "
                            "the router's 2.4 GHz Wi-Fi name and try again.",
                        )
                    if not sent_wifi_password:
                        return SetupOutcome(
                            False,
                            "Camera setup stopped before the Wi-Fi password "
                            "was requested. No password was sent. Keep USB connected.",
                        )
                    if any(
                        marker in current.lower()
                        for marker in (
                            "secrets were required",
                            "authentication failed",
                            "4-way handshake",
                            "pre-shared key",
                        )
                    ):
                        return SetupOutcome(
                            False,
                            "Wi-Fi authentication failed. Check the exact "
                            "2.4 GHz Wi-Fi password and router security. "
                            "USB remains connected.",
                        )
                    return SetupOutcome(
                        False,
                        "The camera did not join home Wi-Fi. Check the "
                        "2.4 GHz Wi-Fi name and password. USB remains connected.",
                    )

            port.write(b"\x03")
            shell_ready = bool(SHELL_PROMPT_RE.search(self._read_prompt(port, 3)))
            return SetupOutcome(
                False,
                "Setup timed out. Leave USB attached for inspection.",
            )
        finally:
            if echo_disabled:
                if not shell_ready:
                    try:
                        port.write(b"\x03")
                        shell_ready = bool(
                            SHELL_PROMPT_RE.search(self._read_prompt(port, 3))
                        )
                    except Exception:
                        shell_ready = False
                if shell_ready:
                    self._restore_terminal_echo(port)

    @staticmethod
    def _restore_terminal_echo(port: serial.Serial) -> None:
        port.write(b"stty echo\n")
        port.flush()

    def _ensure_shell(self, port: serial.Serial, recovery_password: str) -> bool:
        port.write(b"\r\n")
        response = self._read_prompt(port, 10)
        if SHELL_PROMPT_RE.search(response):
            return True
        if not LOGIN_PROMPT_RE.search(response):
            return False

        port.write(self.console_user.encode("utf-8") + b"\n")
        response = self._read_prompt(port, 8)
        if SHELL_PROMPT_RE.search(response):
            return True
        if not PASSWORD_PROMPT_RE.search(response):
            return False

        port.write(recovery_password.encode("utf-8") + b"\n")
        response = self._read_prompt(port, 12)
        return bool(SHELL_PROMPT_RE.search(response))

    @staticmethod
    def _read_prompt(port: serial.Serial, seconds: float) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = port.read(2048)
            if not chunk:
                continue
            data.extend(chunk)
            if (
                SHELL_PROMPT_RE.search(data)
                or LOGIN_PROMPT_RE.search(data)
                or PASSWORD_PROMPT_RE.search(data)
                or b"Login incorrect" in data
            ):
                return bytes(data)
        return bytes(data)


class SshNetworkManagerAdapter:
    """Send a Wi-Fi profile over pinned, key-authenticated SSH."""

    def __init__(
        self,
        host: str,
        user: str,
        identity_file: pathlib.Path,
        host_key_alias: str | None,
        jump_host: str | None,
        known_hosts: pathlib.Path | None,
        network_device: str,
    ) -> None:
        self.host = host
        self.user = user
        self.identity_file = identity_file
        self.host_key_alias = host_key_alias
        self.jump_host = jump_host
        self.known_hosts = known_hosts
        self.network_device = network_device

    def connect(
        self, ssid: str, wifi_password: str, host: str | None = None
    ) -> SetupOutcome:
        target_host = (host or self.host).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", target_host):
            return SetupOutcome(False, "Enter the camera's IP address or host name.")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", self.user):
            return SetupOutcome(False, "The configured SSH camera account is invalid.")
        if not self.identity_file.is_file():
            return SetupOutcome(False, "The camera's SSH key file is unavailable.")
        if self.jump_host and not re.fullmatch(
            r"(?:[A-Za-z_][A-Za-z0-9_-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*",
            self.jump_host,
        ):
            return SetupOutcome(False, "The configured SSH route is invalid.")
        if self.host_key_alias and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]*", self.host_key_alias
        ):
            return SetupOutcome(False, "The configured camera identity is invalid.")

        ssh = shutil.which("ssh")
        if ssh is None:
            return SetupOutcome(False, "OpenSSH is not installed on this computer.")

        inner = network_manager_script(ssid, self.network_device)
        remote_command = (
            "sudo -n sh -c "
            f"{shlex.quote(inner)}; rc=$?; "
            "printf '__VMS_RESULT=%s__\\n' \"$rc\""
        )
        command = [
            ssh,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "NumberOfPasswordPrompts=0",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            str(self.identity_file),
        ]
        alias = self.host_key_alias or target_host
        command.extend(["-o", f"HostKeyAlias={alias}"])
        if self.known_hosts is not None:
            command.extend(["-o", f"UserKnownHostsFile={self.known_hosts}"])
        if self.jump_host:
            proxy = (
                "ssh -W %h:%p -o BatchMode=yes -o StrictHostKeyChecking=yes "
                f"{self.jump_host}"
            )
            command.extend(["-o", f"ProxyCommand={proxy}"])
        command.extend([f"{self.user}@{target_host}", remote_command])

        process: subprocess.Popen[bytes] | None = None
        stdout = bytearray()
        stderr = bytearray()
        output_lock = threading.Lock()
        secret_requested = threading.Event()
        result_received = threading.Event()

        def drain(stream, buffer: bytearray, marker: bytes | None = None) -> None:
            while True:
                chunk = os.read(stream.fileno(), 2048)
                if not chunk:
                    return
                with output_lock:
                    buffer.extend(chunk)
                    current = bytes(buffer)
                if marker and marker in current:
                    secret_requested.set()
                if RESULT_RE.search(current):
                    result_received.set()

        threads: list[threading.Thread] = []
        sent_password = False
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            threads = [
                threading.Thread(
                    target=drain,
                    args=(process.stdout, stdout, WIFI_SECRET_MARKER),
                    daemon=True,
                ),
                threading.Thread(
                    target=drain,
                    args=(process.stderr, stderr),
                    daemon=True,
                ),
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + 20
            while (
                not secret_requested.is_set()
                and not result_received.is_set()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)

            if not secret_requested.is_set():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                for thread in threads:
                    thread.join(timeout=1)
                with output_lock:
                    error_text = bytes(stderr).decode("utf-8", "replace").lower()
                    output_text = bytes(stdout).decode("utf-8", "replace")
                if "password is required" in error_text:
                    return SetupOutcome(
                        False,
                        "Camera SSH connected, but administrator access is not ready. "
                        "No Wi-Fi password was sent.",
                    )
                if RESULT_RE.search(output_text.encode("utf-8")):
                    return self._interpret_result(
                        output_text, output_text.encode("utf-8"), False
                    )
                return SetupOutcome(
                    False,
                    "Camera SSH did not connect. No Wi-Fi password was sent. "
                    "Check its address and saved SSH identity.",
                )

            sent_password = True
            process.stdin.write(wifi_password.encode("utf-8") + b"\n")
            process.stdin.flush()
            process.stdin.close()
            wifi_password = ""
            try:
                process.wait(timeout=95)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                for thread in threads:
                    thread.join(timeout=1)
                return SetupOutcome(
                    False,
                    "The SSH Wi-Fi attempt timed out after sending the key. "
                    "Reconnect and check the camera before retrying.",
                )
            for thread in threads:
                thread.join(timeout=2)
            with output_lock:
                output = bytes(stdout)
            return self._interpret_result(
                output.decode("utf-8", "replace"), output, sent_password
            )
        except Exception:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if sent_password:
                return SetupOutcome(
                    False,
                    "SSH stopped during Wi-Fi setup. Check the camera before retrying.",
                )
            return SetupOutcome(
                False,
                "SSH setup could not connect. No Wi-Fi password was sent.",
            )
        finally:
            wifi_password = ""

    @staticmethod
    def _interpret_result(
        current: str, received: bytes, sent_password: bool
    ) -> SetupOutcome:
        result = RESULT_RE.search(received)
        rc = int(result.group(1)) if result else 1
        connected = "wpa_state=COMPLETED" in current
        has_address = bool(re.search(r"(?m)^ip_address=[0-9.]+\s*$", current))
        modes = re.findall(r"__PROFILE_MODE=([0-7]{3,4})__", current)
        protected = bool(modes) and all(mode == "600" for mode in modes)

        if rc == 0 and connected and has_address and protected:
            return SetupOutcome(
                True,
                "Connected over SSH. The camera has a DHCP address and its "
                "NetworkManager profiles are mode 600.",
            )
        if rc == 0 and connected:
            return SetupOutcome(
                True,
                "Connected to Wi-Fi over SSH. The DHCP or profile-permission "
                "check needs follow-up.",
            )
        if b"__VMS_NO_NETWORK__" in received:
            return SetupOutcome(
                False,
                "The camera cannot see that Wi-Fi name on 2.4 GHz. "
                "Check the spelling and try again.",
            )
        if not sent_password:
            return SetupOutcome(
                False,
                "SSH stopped before requesting the Wi-Fi password. "
                "No Wi-Fi password was sent.",
            )
        if any(
            marker in current.lower()
            for marker in (
                "secrets were required",
                "authentication failed",
                "4-way handshake",
                "pre-shared key",
            )
        ):
            return SetupOutcome(
                False,
                "Wi-Fi authentication failed. Check the 2.4 GHz Wi-Fi password "
                "and router security.",
            )
        return SetupOutcome(False, "The camera did not join home Wi-Fi over SSH.")


class SetupWindow:
    def __init__(self, args: argparse.Namespace) -> None:
        self.adapters: dict[str, SerialNetworkManagerAdapter | SshNetworkManagerAdapter] = {}
        if (
            serial is not None
            and args.recovery_state is not None
            and args.serial_port is not None
        ):
            self.adapters["USB cable"] = SerialNetworkManagerAdapter(
                serial_port=args.serial_port,
                recovery_state=args.recovery_state,
                recovery_field=args.recovery_field,
                camera_number=args.camera_number,
                console_user=args.console_user,
                network_device=args.network_device,
            )
        if args.ssh_key is not None and args.ssh_key.is_file():
            self.adapters["SSH over home Wi-Fi"] = SshNetworkManagerAdapter(
                host=args.ssh_host or "",
                user=args.ssh_user,
                identity_file=args.ssh_key,
                host_key_alias=args.ssh_host_key_alias,
                jump_host=args.ssh_jump_host,
                known_hosts=args.ssh_known_hosts,
                network_device=args.network_device,
            )
        self.camera_name = args.camera_name
        self.root = tk.Tk()
        self.root.title(f"{self.camera_name} Wi-Fi setup")
        self.root.resizable(True, True)
        self.root.minsize(700, 520)
        self.root.geometry("760x620")

        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"Connect {self.camera_name} to home Wi-Fi",
            font=("TkDefaultFont", 14, "bold"),
        ).pack(anchor="w", pady=(0, 10))
        self.transport = tk.StringVar(value=args.selected_transport)
        if len(self.adapters) > 1:
            ttk.Label(frame, text="Connection").pack(anchor="w")
            self.transport_picker = ttk.Combobox(
                frame,
                textvariable=self.transport,
                values=list(self.adapters),
                state="readonly",
                width=30,
            )
            self.transport_picker.pack(anchor="w", pady=(2, 8))
            self.transport_picker.bind("<<ComboboxSelected>>", self.update_transport)
        else:
            self.transport_picker = None

        self.ssh_host = tk.StringVar(value=args.ssh_host or "")
        self.ssh_host_frame = ttk.Frame(frame)
        if "SSH over home Wi-Fi" in self.adapters:
            ttk.Label(self.ssh_host_frame, text="Camera address").pack(anchor="w")
            ttk.Entry(
                self.ssh_host_frame,
                textvariable=self.ssh_host,
                width=40,
            ).pack(anchor="w", pady=(2, 8))

        self.instructions = tk.StringVar(value="")
        self.instructions_label = ttk.Label(
            frame,
            textvariable=self.instructions,
            wraplength=680,
            justify="left",
        )
        self.instructions_label.pack(anchor="w", pady=(0, 16))

        ttk.Label(frame, text="Home Wi-Fi name").pack(anchor="w")
        self.ssid = ttk.Entry(frame, width=58)
        self.ssid.pack(fill="x", pady=(2, 10))
        if args.ssid:
            self.ssid.insert(0, args.ssid)
        ttk.Label(frame, text="Home Wi-Fi password").pack(anchor="w")
        self.password = ttk.Entry(frame, width=58, show="•")
        self.password.pack(fill="x", pady=(2, 12))

        self.status = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status, wraplength=680).pack(
            anchor="w", pady=(0, 10)
        )
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", side="bottom")
        self.send_button = ttk.Button(
            buttons, text="Connect", command=self.start
        )
        self.send_button.pack(side="right")
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", command=self.root.destroy
        )
        self.cancel_button.pack(side="right", padx=(0, 8))
        self.update_transport()
        self.ssid.focus_set()

    def update_transport(self, _event=None) -> None:
        use_ssh = self.transport.get() == "SSH over home Wi-Fi"
        if hasattr(self, "ssh_host_frame"):
            if use_ssh:
                self.ssh_host_frame.pack(fill="x", before=self.instructions_label)
            else:
                self.ssh_host_frame.pack_forget()
        if use_ssh:
            self.instructions.set(
                "The Wi-Fi password is sent through encrypted SSH after the "
                "camera's saved host key is verified. This computer holds it "
                "briefly in memory; it is not put in SSH arguments or logs. "
                "The camera stores it briefly in a root-only RAM file, then "
                "NetworkManager saves the profile on the camera.\n\n"
                "This camera supports 2.4 GHz only."
            )
            self.send_button.configure(text="Send to camera over SSH")
        else:
            self.instructions.set(
                "The Wi-Fi password is sent to the camera over USB. The camera "
                "stores it briefly in a root-only RAM file, then deletes that "
                "file. NetworkManager saves the profile on the camera. This "
                "tool does not print or log the password.\n\n"
                "This camera supports 2.4 GHz only."
            )
            self.send_button.configure(text="Send to camera by USB")

    def start(self) -> None:
        ssid = self.ssid.get()
        password = self.password.get()
        transport = self.transport.get()
        if transport not in self.adapters:
            messagebox.showerror(
                "Connection unavailable",
                "The selected camera connection is not configured.",
                parent=self.root,
            )
            return
        if not ssid or not password:
            messagebox.showerror(
                "Missing information",
                "Enter the Wi-Fi name and password.",
                parent=self.root,
            )
            return
        if "\n" in ssid or "\r" in ssid or "\n" in password or "\r" in password:
            messagebox.showerror(
                "Invalid Wi-Fi information",
                "Enter the Wi-Fi name and password on one line.",
                parent=self.root,
            )
            return
        host = self.ssh_host.get().strip() if transport == "SSH over home Wi-Fi" else ""
        if transport == "SSH over home Wi-Fi" and not host:
            messagebox.showerror(
                "Camera address needed",
                "Enter the camera's address on the home Wi-Fi.",
                parent=self.root,
            )
            return

        self.password.delete(0, tk.END)
        self.ssid.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        if transport == "USB cable":
            self.status.set("Connecting over USB. Keep the camera powered on…")
        else:
            self.status.set("Connecting over SSH. Keep the camera powered on…")
        threading.Thread(
            target=self.connect_camera,
            args=(transport, host, ssid, password),
            daemon=True,
        ).start()

    def connect_camera(
        self, transport: str, host: str, ssid: str, password: str
    ) -> None:
        adapter = self.adapters[transport]
        if isinstance(adapter, SshNetworkManagerAdapter):
            outcome = adapter.connect(ssid, password, host=host)
        else:
            outcome = adapter.connect(ssid, password)
        self.root.after(0, lambda: self.finish(outcome))

    def finish(self, outcome: SetupOutcome) -> None:
        self.status.set(outcome.message)
        self.send_button.configure(
            text="Close", state="normal", command=self.root.destroy
        )

    def run(self) -> None:
        self.root.mainloop()


def discover_serial_port() -> pathlib.Path | None:
    candidates = sorted(
        pathlib.Path(path)
        for path in glob.glob(
            "/dev/serial/by-id/usb-Video_Monitoring_System_Pilot_Recovery_Console_*"
        )
    )
    return candidates[0] if len(candidates) == 1 else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-name", default="Back Porch")
    parser.add_argument("--serial-port", type=pathlib.Path)
    parser.add_argument("--recovery-state", type=pathlib.Path)
    parser.add_argument("--recovery-field", default="pilot_recovery_secret")
    parser.add_argument("--camera-number", type=int)
    parser.add_argument("--console-user", default="vms-recovery")
    parser.add_argument("--network-device", default="wlan0")
    parser.add_argument("--ssid")
    parser.add_argument("--transport", choices=("auto", "usb", "ssh"), default="auto")
    parser.add_argument("--ssh-host")
    parser.add_argument("--ssh-user", default="vms-recovery")
    parser.add_argument("--ssh-key", type=pathlib.Path)
    parser.add_argument("--ssh-host-key-alias")
    parser.add_argument("--ssh-jump-host")
    parser.add_argument("--ssh-known-hosts", type=pathlib.Path)
    args = parser.parse_args()
    if args.serial_port is None and args.recovery_state is not None:
        args.serial_port = discover_serial_port()
    usb_available = (
        serial is not None
        and args.recovery_state is not None
        and args.serial_port is not None
        and args.serial_port.exists()
    )
    ssh_available = args.ssh_key is not None and args.ssh_key.is_file()
    if args.transport == "auto":
        if not usb_available and not ssh_available:
            raise SystemExit(
                "Configure USB recovery state or an SSH key and target camera."
            )
        args.selected_transport = "USB cable" if usb_available else "SSH over home Wi-Fi"
    elif args.transport == "usb":
        if not usb_available:
            raise SystemExit("USB setup needs a connected console and recovery state.")
        args.selected_transport = "USB cable"
    else:
        if not ssh_available:
            raise SystemExit("SSH setup needs a readable per-camera SSH key.")
        args.selected_transport = "SSH over home Wi-Fi"
    return args


if __name__ == "__main__":
    SetupWindow(parse_args()).run()
