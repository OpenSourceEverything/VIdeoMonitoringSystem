#!/bin/bash -e

payload="${ROOTFS_DIR}/usr/share/vms-camera-image"
install -d -m 0755 "${ROOTFS_DIR}/opt/vms" \
    "${ROOTFS_DIR}/usr/local/libexec" \
    "${ROOTFS_DIR}/usr/local/bin" \
    "${ROOTFS_DIR}/etc/systemd/system" \
    "${ROOTFS_DIR}/etc/systemd/journald.conf.d" \
    "${ROOTFS_DIR}/etc/ssh/sshd_config.d" \
    "$payload"
for unit in \
    cloud-init-main.service \
    cloud-init-local.service \
    cloud-init-network.service \
    cloud-config.service \
    cloud-final.service
do
    install -d -m 0755 "${ROOTFS_DIR}/etc/systemd/system/${unit}.d"
done
install -m 0755 files/opt/vms/camera_agent.py "${ROOTFS_DIR}/opt/vms/camera_agent.py"
install -m 0755 files/opt/vms/journal_reader.py "${ROOTFS_DIR}/opt/vms/journal_reader.py"
install -m 0755 files/opt/vms/runtime_metrics.py "${ROOTFS_DIR}/opt/vms/runtime_metrics.py"
install -m 0755 files/opt/vms/network_recovery.py "${ROOTFS_DIR}/opt/vms/network_recovery.py"
install -m 0644 files/etc/systemd/system/vms-camera.service \
    "${ROOTFS_DIR}/etc/systemd/system/vms-camera.service"
install -m 0644 files/etc/systemd/system/vms-network-recovery.service \
    "${ROOTFS_DIR}/etc/systemd/system/vms-network-recovery.service"
install -m 0644 files/etc/systemd/system/vms-network-recovery.timer \
    "${ROOTFS_DIR}/etc/systemd/system/vms-network-recovery.timer"
install -m 0755 files/usr/local/libexec/vms-provision \
    "${ROOTFS_DIR}/usr/local/libexec/vms-provision"
install -m 0644 files/etc/systemd/system/vms-provision.service \
    "${ROOTFS_DIR}/etc/systemd/system/vms-provision.service"
install -m 0700 files/usr/local/libexec/vms-provision-cleanup \
    "${ROOTFS_DIR}/usr/local/libexec/vms-provision-cleanup"
install -m 0644 files/etc/systemd/system/vms-provision-cleanup.service \
    "${ROOTFS_DIR}/etc/systemd/system/vms-provision-cleanup.service"
install -m 0700 files/usr/local/libexec/vms-usb-recovery \
    "${ROOTFS_DIR}/usr/local/libexec/vms-usb-recovery"
install -m 0644 files/etc/systemd/system/vms-usb-recovery.service \
    "${ROOTFS_DIR}/etc/systemd/system/vms-usb-recovery.service"
install -m 0755 files/usr/local/bin/vms-pilot-diagnostics \
    "${ROOTFS_DIR}/usr/local/bin/vms-pilot-diagnostics"
install -m 0644 files/etc/systemd/journald.conf.d/90-vms-pilot-diagnostics.conf \
    "${ROOTFS_DIR}/etc/systemd/journald.conf.d/90-vms-pilot-diagnostics.conf"
install -m 0600 files/etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf \
    "${ROOTFS_DIR}/etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf"
rm -f "${ROOTFS_DIR}/etc/ssh/sshd_config.d/90-vms-pilot-recovery.conf"
for unit in \
    cloud-init-main.service \
    cloud-init-local.service \
    cloud-init-network.service \
    cloud-config.service \
    cloud-final.service
do
    install -m 0644 \
        "files/etc/systemd/system/${unit}.d/99-vms-journal-only.conf" \
        "${ROOTFS_DIR}/etc/systemd/system/${unit}.d/99-vms-journal-only.conf"
done
install -m 0644 files/usr/share/vms-camera-image/build-contract.json \
    "$payload/build-contract.json"
install -m 0644 files/usr/share/vms-camera-image/required-packages.txt \
    "$payload/required-packages.txt"

boot_config="${ROOTFS_DIR}/boot/firmware/config.txt"
if [[ ! -f "$boot_config" ]]; then boot_config="${ROOTFS_DIR}/boot/config.txt"; fi
test -f "$boot_config"
for setting in enable_uart=1 uart_2ndstage=1 dtoverlay=dwc2,dr_mode=peripheral; do
    grep -Fqx "$setting" "$boot_config" || printf '%s\n' "$setting" >> "$boot_config"
done

boot_cmdline="${ROOTFS_DIR}/boot/firmware/cmdline.txt"
if [[ ! -f "$boot_cmdline" ]]; then boot_cmdline="${ROOTFS_DIR}/boot/cmdline.txt"; fi
test -f "$boot_cmdline"
read -r -a current_arguments < "$boot_cmdline"
filtered_arguments=()
for argument in "${current_arguments[@]}"; do
    case "$argument" in
        quiet|splash|plymouth.ignore-serial-consoles|console=serial0,*|loglevel=*|systemd.log_level=*|systemd.show_status=*) ;;
        *) filtered_arguments+=("$argument") ;;
    esac
done
filtered_arguments+=(
    console=serial0,115200
    loglevel=4
    systemd.show_status=1
)
printf '%s\n' "${filtered_arguments[*]}" > "$boot_cmdline"

on_chroot <<'VMS_CHROOT'
set -eu
while IFS= read -r package; do
    test -n "$package"
    dpkg-query -W -f='${db:Status-Abbrev}\n' "$package" | grep -Fx 'ii '
done < /usr/share/vms-camera-image/required-packages.txt
python3 -c 'import picamera2'
command -v avahi-daemon >/dev/null
command -v nmcli >/dev/null
command -v ping >/dev/null
command -v sshd >/dev/null
for generated in \
    /usr/local/libexec/vms-first-boot \
    /etc/systemd/system/vms-first-boot.service
do
    if test -e "$generated" || test -L "$generated"; then
        echo "Generic image contains a per-device first-boot placeholder" >&2
        exit 1
    fi
done
systemctl mask userconfig.service
systemctl enable \
    ssh.service \
    vms-usb-recovery.service \
    vms-provision.service \
    vms-provision-cleanup.service
systemctl disable serial-getty@serial0.service 2>/dev/null || true
systemctl disable vms-camera.service 2>/dev/null || true
systemctl disable vms-network-recovery.timer 2>/dev/null || true
test "$(systemctl is-enabled vms-provision.service)" = enabled
test "$(systemctl is-enabled vms-provision-cleanup.service)" = enabled
systemd-analyze verify \
    cloud-init-main.service \
    cloud-init-local.service \
    cloud-init-network.service \
    cloud-config.service \
    cloud-final.service \
    vms-usb-recovery.service \
    vms-network-recovery.service \
    vms-network-recovery.timer \
    vms-provision.service \
    vms-provision-cleanup.service
for unit in \
    cloud-init-main.service \
    cloud-init-local.service \
    cloud-init-network.service \
    cloud-config.service \
    cloud-final.service
do
    drop_in="/etc/systemd/system/${unit}.d/99-vms-journal-only.conf"
    test "$(stat -c '%U:%G:%a' "$drop_in")" = root:root:644
    test "$(sed -n '/^StandardOutput=/p' "$drop_in")" = StandardOutput=journal
    test "$(sed -n '/^StandardError=/p' "$drop_in")" = StandardError=journal
    if grep -Fq console "$drop_in"; then
        echo "Cloud-init journal-only drop-in still names a console" >&2
        exit 1
    fi
    effective_unit=$(systemd-analyze cat-config "systemd/system/$unit")
    effective_stdout=$(printf '%s\n' "$effective_unit" | awk '
        /^\[/ { section=$0 }
        section == "[Service]" && /^StandardOutput=/ {
            sub(/^StandardOutput=/, ""); value=$0
        }
        END { print value }
    ')
    effective_stderr=$(printf '%s\n' "$effective_unit" | awk '
        /^\[/ { section=$0 }
        section == "[Service]" && /^StandardError=/ {
            sub(/^StandardError=/, ""); value=$0
        }
        END { print value }
    ')
    test "$effective_stdout" = journal
    test "$effective_stderr" = journal
done
test "$(stat -c '%U:%G:%a' /usr/local/libexec/vms-provision)" = root:root:755
test "$(stat -c '%U:%G:%a' /usr/local/libexec/vms-provision-cleanup)" = root:root:700
if id vms-build >/dev/null 2>&1; then userdel --remove vms-build; fi
while IFS= read -r package; do
    dpkg-query -W -f='${binary:Package}\t${Version}\n' "$package"
done < /usr/share/vms-camera-image/required-packages.txt \
    > /usr/share/vms-camera-image/package-versions.tsv
sha256sum \
    /opt/vms/camera_agent.py \
    /opt/vms/journal_reader.py \
    /opt/vms/runtime_metrics.py \
    /opt/vms/network_recovery.py \
    /etc/systemd/system/vms-camera.service \
    /etc/systemd/system/vms-network-recovery.service \
    /etc/systemd/system/vms-network-recovery.timer \
    /usr/local/libexec/vms-provision \
    /etc/systemd/system/vms-provision.service \
    /usr/local/libexec/vms-provision-cleanup \
    /etc/systemd/system/vms-provision-cleanup.service \
    /usr/local/libexec/vms-usb-recovery \
    /etc/systemd/system/vms-usb-recovery.service \
    /usr/local/bin/vms-pilot-diagnostics \
    /etc/systemd/journald.conf.d/90-vms-pilot-diagnostics.conf \
    /etc/ssh/sshd_config.d/00-vms-pilot-recovery.conf \
    /etc/systemd/system/cloud-init-main.service.d/99-vms-journal-only.conf \
    /etc/systemd/system/cloud-init-local.service.d/99-vms-journal-only.conf \
    /etc/systemd/system/cloud-init-network.service.d/99-vms-journal-only.conf \
    /etc/systemd/system/cloud-config.service.d/99-vms-journal-only.conf \
    /etc/systemd/system/cloud-final.service.d/99-vms-journal-only.conf \
    > /usr/share/vms-camera-image/payload.sha256
VMS_CHROOT
