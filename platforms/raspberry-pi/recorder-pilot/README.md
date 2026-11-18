# Raspberry Pi Recorder Pilot

This legacy development path installs the generic Linux recorder manually on a
Pi 4. New complete-kit tests use `tools/build-recorder-image` and the factory
GUI instead. This path remains useful for recovery and installer development.

From the repository root:

```sh
platforms/raspberry-pi/recorder-pilot/prepare-card \
    /dev/mmcblk0 ~/.ssh/id_ed25519.pub
```

The command verifies the pinned image, requires the exact card serial before
erasing, and installs the selected owner public key. No login password or
fixed repository key is created. The matching private key must already be
loaded in `ssh-agent` so the command cannot accidentally install an unusable
key. This development recovery access is explicit and removable; it is not a
product backdoor.

After first boot, transfer this source tree and run the generic installer:

```sh
sudo platforms/linux/recorder/install-recorder
```

This is a physical development gate, not the final no-shell customer setup.
