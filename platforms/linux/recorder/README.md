# Linux Recorder Service

This package runs the recorder portal under systemd from the same Python
package used by the CLI and GUI.

For a development installation from this checkout:

```sh
sudo platforms/linux/recorder/install-recorder
systemctl status vms-recorder
```

The appliance service listens on the home LAN at
`http://osecameras.local`. The stable local name is printed by the installer
and belongs on the recorder's QR label; owners do not discover or remember an
IP address. The owner password protects the portal and private media. Never
forward the portal through the home router.

For development before installing the appliance service, the default CLI
server remains loopback-only and can be reached through an explicit SSH tunnel:

```sh
ssh -L 9081:127.0.0.1:9081 recorder-host
```

Then open `http://127.0.0.1:9081`.

Run `sudo vms remote setup` to choose private Tailscale access or the owner's
own droplet. Run `sudo vms remote status` whenever the owner needs the local
and remote browser addresses. The owner-facing command supplies the appliance
data directory automatically; internal service commands remain hidden.

An attached ext4 volume labeled `VMS_RECORDING` is mounted automatically at
`/mnt/VMS_RECORDING` with executable files, device nodes, and set-user-ID bits
disabled. The portal still starts when the drive is absent. Formatting remains
an explicit appliance/setup operation; this service never erases a disk.

The recording timer starts one bounded cycle at a time. Each cycle reloads the
current recorder state, resolves the initialized volume and private camera
identities through product adapters, then records authorized cameras
concurrently. Missing storage or cameras does not prevent portal access.
The separate daily maintenance timer enforces the owner-selected retention
period by atomically retiring only validated product recording segments;
unrelated files on an adopted volume are outside its scope.

The independent `vms-recorder-alerts.service` polls confirmed events, including
while recording is paused. Its worker does not wait for a capture cycle. Push
acceptance is reported separately from phone display, which is not measured.
The worker keeps Python imports warm, reloads owner settings each cycle and
waits two seconds after each completed poll. Only status changes are logged.
See [notification diagnosis](../../../docs/design.md#notifications).

The independent `vms-recorder-monitor.service` samples authenticated camera
identity, snapshots, native stream capability, Wi-Fi recovery, power, CPU
temperature/load and recent retained-video timestamps once a minute. Missing
hardware measurements remain unknown. It keeps 120 samples and 256 state
changes in owner-only `monitoring/health-history.json` under the recorder data
directory. Logs contain warning codes rather than credentials or addresses.
The worker probes through the camera adapters and never restarts cameras or
changes recording policy. Native camera agents expose matching runtime fields
through authenticated `/health`.

Owners can open Settings -> System log in the password-protected portal to view
the same timestamped measurements, choose a prior snapshot and download their
machine-readable JSON export. Raw service logs are available from that page
after the diagnostic update is installed. They provide bounded, read-only
tails for fixed recorder services and, when a camera agent supports the
endpoint, camera services. The portal does not accept arbitrary units or
commands; credentials are redacted before display and export.

For a different sampling interval, thresholds or history bounds, pass a JSON
file with `--config` to `python -m video_monitoring_system.monitoring`; supported
keys are `interval_s`, `temperature_c`, `load_per_core`, `samples` and `events`.
`--once` collects one sample. Previous boots require persistent system journals
on the appliance; monitoring cannot reconstruct an outage that predates logging.

Use `vms storage init` for an empty prepared volume. Use `vms storage adopt`
to add the product layout while preserving unrelated files already present on
an explicitly selected, adapter-attested volume.
