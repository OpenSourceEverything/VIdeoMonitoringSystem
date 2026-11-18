# Camera onboarding tools

`usb_wifi_setup.py` is a local pilot utility for commissioning a Raspberry Pi
camera over USB serial or authenticated SSH. The same Wi-Fi form works with
either transport. Keep this code in the VMS source repository for reuse; it is
not the consumer onboarding flow.

## Security and limits

- This is a pilot service path, not the consumer onboarding UI. It is used when
  the camera's DPP exchange cannot complete.
- The owner enters the Wi-Fi password into a masked local field. The computer
  holds it briefly in memory and sends it over USB or encrypted SSH after the
  camera requests it. SSH verifies the camera's saved host key and uses its
  per-camera key. The camera briefly writes the Wi-Fi key to a root-only file
  under `/run` (RAM), uses NetworkManager's `passwd-file` option to connect,
  then deletes the temporary file. The password is not a command-line argument
  and this tool does not print or log it.
- NetworkManager saves the profile on the camera. The helper checks that the
  camera obtains a DHCP address and that its saved NetworkManager profiles use
  file mode 600.
- The camera's setup sessions have no NetworkManager secret agent, so this tool
  passes the key through a private temporary file instead of an interactive
  NetworkManager prompt.
- It first creates a temporary profile and preserves existing profiles until
  home Wi-Fi association succeeds. It then stores the active profile under
  `vms-home-NNN`, removes the old recorder profile, and cleans up inactive
  duplicates for the selected SSID.
- USB reads the camera recovery secret from a private JSON file supplied at
  launch. SSH uses a private per-camera key and a previously verified host key.
  Never put keys, recovery files or Wi-Fi credentials in this repository.
- Keep the camera reachable over its selected transport until association,
  recorder reachability, and saved-video checks pass. A failed setup does not
  restore the recorder access point.

## Run

### USB first; re-image only if needed

For a camera that already boots a compatible OSE image, USB serial can set its
home Wi-Fi; swapping or rewriting the SD card is unnecessary. The helper must
confirm the existing recovery login before it sends Wi-Fi settings. If login
is not confirmed, stop and recover the camera's supported credentials or
back up and inspect its card before considering a re-image.

For USB, use a private camera recovery-state file and the camera's serial cable:

```text
python3 tools/camera-onboarding/usb_wifi_setup.py \
  --recovery-state /path/to/private/camera-recovery-state.json
```

The helper auto-detects the OSE pilot USB console. Supply `--serial-port` for a
different supported serial adapter, and `--console-user` or
`--network-device` for a different camera image. The Wi-Fi password must be
entered only in the local form; never add it to command arguments or chat.

For SSH, supply the camera address, its private key, and the known-host alias
that already contains that camera's verified SSH host key:

```text
python3 tools/camera-onboarding/usb_wifi_setup.py \
  --transport ssh \
  --ssh-host 192.0.2.10 \
  --ssh-key /path/to/private/camera-key \
  --ssh-host-key-alias verified-camera-name
```

Add `--ssh-jump-host vms-recovery@osecameras.local` only when the workstation
must reach a camera through the recorder. SSH uses noninteractive key
authentication and password-free camera administrator access. It refuses an
unknown or changed host key. When both transports are configured, the prompt
offers a USB/SSH selector. The camera must already be reachable over the
selected network path; use USB for its first move to home Wi-Fi when SSH cannot
reach it.

The product setup flow remains transport-independent. Production onboarding
must use a reviewed secure adapter and must not require a development computer,
USB serial, or operator recovery password.
