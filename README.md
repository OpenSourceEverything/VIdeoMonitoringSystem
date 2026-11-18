# Video Monitoring System

An open, local camera system designed to ship as a recorder with pre-enrolled cameras.

## Hardware gate

Do not develop or port VMS software on hardware unless its exact configuration
passes every mandatory item in
[the product requirements](docs/requirements/requirements.md). Unknown or failed
hardware is not a development target; existing pilot status grants no exception.
Controller prices rank sourcing options, not eligibility; camera cost limits remain.

## The system in one minute

Security systems commonly put most detection in the camera, in a local
recorder, or in a vendor cloud. These are useful workload descriptions, not
mutually exclusive product categories. OSE's chosen product path is **local
recorder-led**: a finished camera sends a native H.264 main stream for recording
and a smaller stream for motion/person detection; the recorder owns storage,
events, playback, alerts, setup, and diagnostics. It does not routinely
re-encode camera video. Camera-provided detections may be accepted later as
extra evidence, not as a second architecture to build in parallel. See the
[architecture and its acceptance boundary](docs/design.md#architecture).

Customers see only:

```text
Feeds | Events | Cameras | Recorder | Storage
```

They do not need to manage operating-system images, addresses, accounts, or network configuration.

## Product

- The current Raspberry Pi 4/Zero installation is a software pilot, not the
  hardware planned for sale.
- The product candidate is a finished Linux recorder with qualified finished
cameras meeting the [product requirements](docs/requirements/requirements.md);
  no exact production hardware is approved yet.
- Eight cameras are a product target, not a tested capacity claim.
- Internet connection: optional; recording and live viewing remain local.
- Alert delivery: optional email, text message, or outbound server connection.
- OSE-controlled devices use signed software and unique identity. Third-party
  finished cameras keep their own firmware and need a tested local setup path.

## Commands

The graphical interface and command line use the same application operations.

```text
vms setup
vms provision
vms provision --config templates/provisioning/camera.example.yaml --flash
vms storage init
vms storage adopt
vms camera list --unclaimed
vms camera claim <camera-id> --name Driveway
vms camera recording <camera-id> --no-enabled
vms camera replace Driveway <new-camera-id>
vms camera remove Driveway
vms recording set --mode continuous --days 7
vms recording list
vms recording maintain
vms recorder serve
vms backup create recorder.vms-backup
vms --data-dir /new/empty/state backup restore recorder.vms-backup
vms remote setup
vms remote status
vms status
vms update

vms dev doctor
vms kit verify
```

Original Raspberry Pi Zero W and Zero 2 W boards are legacy pilot hardware, not
approved product or development targets.

The current implementation is a development pilot. Product claims are governed
by the documented [acceptance gates](docs/requirements/requirements.md#acceptance), not by the presence of
code or a development image.

Platform packaging lives under `platforms/`; temporary legacy compatibility
lives under `adapters/`. Concrete installations and encrypted deployment
values belong in separate deployment repositories.

The development Linux recorder service is documented in
[`platforms/linux/recorder`](platforms/linux/recorder/README.md). The bare CLI
is loopback-only by default; the appliance service publishes the
password-protected portal as `osecameras.local` on the home LAN. The
[remote-access contract](docs/design.md#remote-access) offers the owner's personal
Tailscale network or the owner's own droplet, never a shared product SaaS. The
Recorder page downloads a password-encrypted backup of
settings, camera access, diagnostics, and the complete recording catalog.
Video payloads remain on the recording drive and are explicitly not included.
Restore refuses to overwrite an existing application data directory.

## Development

Use a Git checkout for development. Image-building tools select versioned files,
and the full test suite exercises that contract. A downloaded source archive can
build the Python package but lacks the Git metadata required by those checks.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --editable .
.venv/bin/python -m unittest discover -s tests
.venv/bin/vms dev doctor
```

These Linux commands explicitly use this checkout's virtual environment; no
global pip installation or manual activation is needed. On Debian/Ubuntu,
install python3-venv first if creating the environment reports missing ensurepip.
The commands above test the local checkout, not an existing home recorder.

Start with [current state and next actions](docs/README.md).
The documentation has one requirements file, one [current design](docs/design.md),
current research, and an archive of research/review/measurement evidence only.
[Product requirements](docs/requirements/requirements.md) are the single hardware and
system specification; [current sourcing](docs/research/README.md) records
offers and unresolved evidence.
[Phase 2](docs/design.md#four-camera-deployment) is the four-camera property deployment;
[hardware and BOM](docs/design.md#hardware-and-bom) shows the parts and buying gates.

## License

OSE-authored software, firmware, tests and configuration are GPL-3.0-or-later.
OSE-authored explanatory documentation is CC BY-SA 4.0. Third-party material
retains its original terms; the pi-gen patches include BSD-3-Clause upstream
context. LICENSING.md maps the scopes, and THIRD_PARTY_NOTICES.md identifies
the upstream material and separately installed dependencies.

LICENSE and LICENSES/ contain the license texts. No trademark rights or product
certification are granted. Source availability is not a hardware qualification
or an approval to redistribute a complete operating-system image.
