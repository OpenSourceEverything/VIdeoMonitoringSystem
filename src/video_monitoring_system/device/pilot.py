"""Static recovery assets included only in the pilot camera image."""

PILOT_USB_RECOVERY_SCRIPT = r"""#!/bin/sh
set -eu

sys_root=${VMS_USB_RECOVERY_SYS_ROOT:-/sys}
dev_root=${VMS_USB_RECOVERY_DEV_ROOT:-/dev}
machine_id_file=${VMS_USB_RECOVERY_MACHINE_ID_FILE:-/etc/machine-id}
configfs="$sys_root/kernel/config"
udc_class="$sys_root/class/udc"
gadget_root="$configfs/usb_gadget"
gadget="$gadget_root/vms-pilot-recovery"
configuration="$gadget/configs/c.1"
function="$gadget/functions/acm.usb0"
function_link="$configuration/acm.usb0"
tty="$dev_root/ttyGS0"
udc_wait_attempts=30
tty_wait_attempts=20
if [ "${VMS_USB_RECOVERY_TEST_ALLOW_NONCHAR_TTY:-0}" = 1 ] &&
    [ "$sys_root" != /sys ] && [ "$dev_root" != /dev ]; then
    udc_wait_attempts=${VMS_USB_RECOVERY_TEST_UDC_WAIT_ATTEMPTS:-30}
    tty_wait_attempts=${VMS_USB_RECOVERY_TEST_TTY_WAIT_ATTEMPTS:-20}
fi
case "$udc_wait_attempts" in ''|*[!0-9]*|0) exit 2 ;; esac
case "$tty_wait_attempts" in ''|*[!0-9]*|0) exit 2 ;; esac
ready=0

current_udc() {
    if [ -r "$gadget/UDC" ]; then
        cat "$gadget/UDC" 2>/dev/null || true
    fi
}

foreign_owner() {
    requested_udc=$1
    for owner_file in "$gadget_root"/*/UDC; do
        [ -r "$owner_file" ] || continue
        owner_gadget=${owner_file%/UDC}
        [ "$owner_gadget" = "$gadget" ] && continue
        owner_udc=$(cat "$owner_file" 2>/dev/null || true)
        if [ "$owner_udc" = "$requested_udc" ]; then
            printf '%s\n' "${owner_gadget##*/}"
            return 0
        fi
    done
    return 1
}

choose_udc() {
    preferred_udc=$1
    fallback_udc=
    conflicts=
    saw_udc=0
    for udc_path in "$udc_class"/*; do
        [ -e "$udc_path" ] || continue
        saw_udc=1
        candidate_udc=${udc_path##*/}
        if owner=$(foreign_owner "$candidate_udc"); then
            conflicts="${conflicts}${conflicts:+, }$candidate_udc ($owner)"
            continue
        fi
        if [ "$candidate_udc" = "$preferred_udc" ]; then
            printf '%s\n' "$candidate_udc"
            return 0
        fi
        [ -n "$fallback_udc" ] || fallback_udc=$candidate_udc
    done
    if [ -n "$fallback_udc" ]; then
        printf '%s\n' "$fallback_udc"
        return 0
    fi
    if [ "$saw_udc" -eq 1 ]; then
        printf 'No USB device controller is free; owned controllers: %s\n' \
            "$conflicts" >&2
        return 2
    fi
    return 1
}

gadget_contract_is_complete() {
    [ "$(cat "$gadget/idVendor" 2>/dev/null || true)" = 0x1d6b ] || return 1
    [ "$(cat "$gadget/idProduct" 2>/dev/null || true)" = 0x0104 ] || return 1
    [ "$(cat "$gadget/strings/0x409/serialnumber" 2>/dev/null || true)" = \
        "$serial_number" ] || return 1
    [ "$(cat "$gadget/strings/0x409/manufacturer" 2>/dev/null || true)" = \
        "Video Monitoring System" ] || return 1
    [ "$(cat "$gadget/strings/0x409/product" 2>/dev/null || true)" = \
        "Pilot Recovery Console" ] || return 1
    [ "$(cat "$configuration/strings/0x409/configuration" 2>/dev/null || true)" = \
        "USB serial recovery" ] || return 1
    [ -d "$function" ] || return 1
    [ -L "$function_link" ] || return 1
    [ "$(readlink -f "$function_link" 2>/dev/null || true)" = "$function" ] || \
        return 1
    for owned_configuration in "$gadget/configs"/*; do
        [ -d "$owned_configuration" ] || continue
        [ "$owned_configuration" = "$configuration" ] || return 1
    done
    for owned_function in "$gadget/functions"/*; do
        [ -d "$owned_function" ] || continue
        [ "$owned_function" = "$function" ] || return 1
    done
    for configured_function in "$configuration"/*; do
        [ -L "$configured_function" ] || continue
        [ "$configured_function" = "$function_link" ] || return 1
    done
    return 0
}

recovery_is_ready() {
    expected_udc=$1
    [ "$(current_udc)" = "$expected_udc" ] || return 1
    [ -c "$tty" ] && return 0
    [ "${VMS_USB_RECOVERY_TEST_ALLOW_NONCHAR_TTY:-0}" = 1 ] &&
        [ "$sys_root" != /sys ] &&
        [ "$dev_root" != /dev ] &&
        [ -e "$tty" ]
}

wait_until_ready() {
    expected_udc=$1
    attempt=0
    while [ "$attempt" -lt "$tty_wait_attempts" ]; do
        recovery_is_ready "$expected_udc" && return 0
        attempt=$((attempt + 1))
        [ "$attempt" -lt "$tty_wait_attempts" ] && sleep 1
    done
    return 1
}

unbind_owned_gadget() {
    bound_udc=$(current_udc)
    [ -n "$bound_udc" ] || return 0
    printf '\n' > "$gadget/UDC"
    [ -z "$(current_udc)" ]
}

rollback() {
    code=$?
    trap - EXIT HUP INT TERM
    if [ "$ready" -ne 1 ] && [ -e "$gadget/UDC" ]; then
        bound_udc=$(current_udc)
        if [ -n "$bound_udc" ]; then
            printf '\n' > "$gadget/UDC" 2>/dev/null || true
        fi
    fi
    exit "$code"
}

modprobe dwc2
modprobe libcomposite
mountpoint -q "$configfs" || mount -t configfs configfs "$configfs"
serial_number=$(tr -d '\n' < "$machine_id_file")
if [ -z "$serial_number" ]; then
    printf 'The machine ID required for the USB serial number is empty.\n' >&2
    exit 1
fi

existing_udc=$(current_udc)
udc=
attempt=0
while [ "$attempt" -lt "$udc_wait_attempts" ]; do
    if udc=$(choose_udc "$existing_udc"); then
        break
    else
        choice_status=$?
    fi
    [ "$choice_status" -ne 2 ] || exit 1
    attempt=$((attempt + 1))
    [ "$attempt" -lt "$udc_wait_attempts" ] && sleep 1
done
if [ -z "$udc" ]; then
    printf 'No USB device controller appeared after %s attempts.\n' \
        "$udc_wait_attempts" >&2
    exit 1
fi

trap rollback EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$existing_udc" = "$udc" ] && gadget_contract_is_complete; then
    if wait_until_ready "$udc"; then
        systemctl --no-block start serial-getty@ttyGS0.service
        recovery_is_ready "$udc"
        ready=1
        trap - EXIT HUP INT TERM
        printf 'VMS pilot USB recovery ready on UDC %s.\n' "$udc"
        exit 0
    fi
fi

unbind_owned_gadget
mkdir -p "$gadget"
printf '0x1d6b\n' > "$gadget/idVendor"
printf '0x0104\n' > "$gadget/idProduct"
mkdir -p "$gadget/strings/0x409"
printf '%s\n' "$serial_number" > "$gadget/strings/0x409/serialnumber"
printf 'Video Monitoring System\n' > "$gadget/strings/0x409/manufacturer"
printf 'Pilot Recovery Console\n' > "$gadget/strings/0x409/product"
mkdir -p "$configuration/strings/0x409"
printf 'USB serial recovery\n' > "$configuration/strings/0x409/configuration"
mkdir -p "$function"

for owned_configuration in "$gadget/configs"/*; do
    [ -d "$owned_configuration" ] || continue
    if [ "$owned_configuration" != "$configuration" ]; then
        printf 'Unexpected configuration in the VMS recovery gadget: %s\n' \
            "$owned_configuration" >&2
        exit 1
    fi
done
for owned_function in "$gadget/functions"/*; do
    [ -d "$owned_function" ] || continue
    if [ "$owned_function" != "$function" ]; then
        printf 'Unexpected function in the VMS recovery gadget: %s\n' \
            "$owned_function" >&2
        exit 1
    fi
done
for configured_function in "$configuration"/*; do
    [ -L "$configured_function" ] || continue
    if [ "$configured_function" != "$function_link" ]; then
        printf 'Unexpected function link in the VMS recovery gadget: %s\n' \
            "$configured_function" >&2
        exit 1
    fi
done
if [ -L "$function_link" ]; then
    if [ "$(readlink -f "$function_link" 2>/dev/null || true)" != "$function" ]; then
        rm -f "$function_link"
    fi
elif [ -e "$function_link" ]; then
    printf 'The VMS recovery function path is not a symbolic link.\n' >&2
    exit 1
fi
[ -L "$function_link" ] || ln -s "$function" "$function_link"

if owner=$(foreign_owner "$udc"); then
    printf 'USB device controller %s was claimed by gadget %s.\n' \
        "$udc" "$owner" >&2
    exit 1
fi

# Binding is deliberately the final configuration write. Until this succeeds,
# a partial or interrupted setup cannot be exposed to the USB host.
printf '%s\n' "$udc" > "$gadget/UDC"

if ! wait_until_ready "$udc"; then
    printf 'USB recovery did not create %s on bound UDC %s.\n' "$tty" "$udc" >&2
    exit 1
fi
systemctl --no-block start serial-getty@ttyGS0.service
recovery_is_ready "$udc"
ready=1
trap - EXIT HUP INT TERM
printf 'VMS pilot USB recovery ready on UDC %s.\n' "$udc"
"""

PILOT_USB_RECOVERY_UNIT = """[Unit]
Description=VMS pilot USB recovery console
After=local-fs.target systemd-modules-load.service
Before=serial-getty@ttyGS0.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=true
ExecStart=/usr/local/libexec/vms-usb-recovery
Restart=on-failure
RestartSec=15s
TimeoutStartSec=75s

[Install]
WantedBy=multi-user.target
"""

# Preserve the existing public name while treating this as a complete unit contract.
PILOT_USB_RECOVERY_SERVICE = PILOT_USB_RECOVERY_UNIT

PILOT_DIAGNOSTICS_SCRIPT = """#!/bin/sh
set -u
printf '%s\n' 'VMS pilot diagnostics'
printf '%s\n' '--- provisioning state ---'
for status in /var/lib/vms/first-boot-status.json /var/lib/vms/first-boot.json; do
    if [ -f "$status" ]; then cat "$status"; fi
done
printf '%s\n' '--- devices ---'
ip -brief address 2>/dev/null || true
nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null || true
printf '%s\n' '--- power ---'
vcgencmd get_throttled 2>/dev/null || true
printf '%s\n' '--- failed services ---'
systemctl --no-pager --failed 2>/dev/null || true
printf '%s\n' '--- VMS journal ---'
journalctl --no-pager -b -u vms-provision.service -u vms-provision-cleanup.service -u vms-first-boot.service -u vms-usb-recovery.service -u vms-camera.service -u NetworkManager.service 2>/dev/null || true
printf '%s\n' '--- recent kernel journal ---'
journalctl --no-pager -b -k -n 200 2>/dev/null || true
"""

PILOT_JOURNALD_CONFIG = """[Journal]
Storage=persistent
Compress=yes
SystemMaxUse=128M
MaxRetentionSec=30day
MaxLevelStore=debug
"""

PILOT_SSH_CONFIG = """PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
AuthorizedKeysFile .ssh/authorized_keys
UsePAM yes
AllowUsers vms-recovery
"""


__all__ = [
    "PILOT_DIAGNOSTICS_SCRIPT",
    "PILOT_JOURNALD_CONFIG",
    "PILOT_SSH_CONFIG",
    "PILOT_USB_RECOVERY_SCRIPT",
    "PILOT_USB_RECOVERY_SERVICE",
    "PILOT_USB_RECOVERY_UNIT",
]
