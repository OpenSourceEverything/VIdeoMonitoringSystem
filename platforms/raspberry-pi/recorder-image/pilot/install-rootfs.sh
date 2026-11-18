#!/bin/bash -e

payload="${ROOTFS_DIR}/usr/share/vms-recorder-image"
source_tree="${ROOTFS_DIR}/usr/src/video-monitoring-system"
wheelhouse="${ROOTFS_DIR}/usr/share/vms-recorder-wheels"

install -d -m 0755 "$payload" "$source_tree" "$wheelhouse" \
    "${ROOTFS_DIR}/usr/local/libexec" \
    "${ROOTFS_DIR}/etc/systemd/system"
cp -a files/usr/src/video-monitoring-system/. "$source_tree/"
cp -a files/usr/share/vms-recorder-wheels/. "$wheelhouse/"
install -m 0755 files/usr/local/libexec/vms-provision \
    "${ROOTFS_DIR}/usr/local/libexec/vms-provision"
install -m 0644 files/etc/systemd/system/vms-provision.service \
    "${ROOTFS_DIR}/etc/systemd/system/vms-provision.service"
install -m 0700 files/usr/local/libexec/vms-provision-cleanup \
    "${ROOTFS_DIR}/usr/local/libexec/vms-provision-cleanup"
install -m 0644 files/etc/systemd/system/vms-provision-cleanup.service \
    "${ROOTFS_DIR}/etc/systemd/system/vms-provision-cleanup.service"
install -m 0644 files/usr/share/vms-recorder-image/build-contract.json \
    "$payload/build-contract.json"
install -m 0644 files/usr/share/vms-recorder-image/required-packages.txt \
    "$payload/required-packages.txt"
install -m 0644 files/usr/share/vms-recorder-image/python-wheels.json \
    "$payload/python-wheels.json"

for unit in \
    cloud-init-main.service \
    cloud-init-local.service \
    cloud-init-network.service \
    cloud-config.service \
    cloud-final.service
do
    directory="${ROOTFS_DIR}/etc/systemd/system/${unit}.d"
    install -d -m 0755 "$directory"
    install -m 0644 \
        "files/etc/systemd/system/${unit}.d/99-vms-journal-only.conf" \
        "$directory/99-vms-journal-only.conf"
done

on_chroot <<'VMS_CHROOT'
set -eu
while IFS= read -r package; do
    test -n "$package"
    dpkg-query -W -f='${db:Status-Abbrev}\n' "$package" | grep -Fx 'ii '
done < /usr/share/vms-recorder-image/required-packages.txt
VMS_INSTALL_MODE=image \
VMS_WHEELHOUSE=/usr/share/vms-recorder-wheels \
    /usr/src/video-monitoring-system/platforms/linux/recorder/install-recorder
/opt/video-monitoring-system/bin/python - <<'PY'
import importlib.metadata
import json
from pathlib import Path

expected = json.loads(
    Path("/usr/share/vms-recorder-image/python-wheels.json").read_text(
        encoding="utf-8"
    )
)
for item in expected:
    if item["name"] == "setuptools":
        continue
    actual = importlib.metadata.version(item["name"])
    if actual != item["version"]:
        raise SystemExit(f"{item['name']} version mismatch: {actual}")
PY
for generated in \
    /etc/vms/device.json \
    /usr/local/libexec/vms-first-boot \
    /etc/systemd/system/vms-first-boot.service
do
    if test -e "$generated" || test -L "$generated"; then
        echo "Generic recorder image contains per-kit state" >&2
        exit 1
    fi
done
systemctl mask userconfig.service
systemctl enable vms-provision.service vms-provision-cleanup.service
systemctl disable \
    vms-recorder.service \
    vms-recorder-capture.timer \
    vms-recorder-alerts.service \
    vms-recorder-monitor.service \
    vms-recorder-maintenance.timer \
    vms-recording-ready.path \
    vms-remote-gateway.path >/dev/null 2>&1 || true
test "$(systemctl is-enabled vms-provision.service)" = enabled
test "$(systemctl is-enabled vms-provision-cleanup.service)" = enabled
systemd-analyze verify \
    vms-provision.service \
    vms-provision-cleanup.service \
    vms-recorder.service \
    vms-recorder-capture.timer \
    vms-recorder-alerts.service \
    vms-recorder-monitor.service \
    vms-recorder-maintenance.timer
if id vms-build >/dev/null 2>&1; then userdel --remove vms-build; fi
while IFS= read -r package; do
    dpkg-query -W -f='${binary:Package}\t${Version}\n' "$package"
done < /usr/share/vms-recorder-image/required-packages.txt \
    > /usr/share/vms-recorder-image/package-versions.tsv
rm -rf /usr/src/video-monitoring-system /usr/share/vms-recorder-wheels
VMS_CHROOT
