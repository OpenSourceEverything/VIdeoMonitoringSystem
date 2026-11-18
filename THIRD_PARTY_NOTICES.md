# Third-party notices and distribution boundary

VMS source is not a bundled operating system, Frigate container, AI model or
camera firmware image. Dependencies fetched or installed separately keep their
own licenses. Preserve their actual version-specific notices when distributing
a wheelhouse, container or appliance image; this file is not a substitute.

## Included pi-gen source context

The pi-gen-*.patch files in platforms/raspberry-pi/ contain modified/context
lines from Raspberry Pi's pi-gen. The image manifests pin their corresponding
builder revisions, rather than relying on an unversioned upstream branch.

Copyright (c) 2015 Raspberry Pi (Trading) Ltd. All rights reserved.
License: BSD-3-Clause. Complete upstream text:
LICENSES/pi-gen-BSD-3-Clause.txt

Camera builder source and license:
https://github.com/RPi-Distro/pi-gen/tree/314262cb286b8f33327a6f0cbabe14c625021ca0
https://github.com/RPi-Distro/pi-gen/blob/314262cb286b8f33327a6f0cbabe14c625021ca0/LICENSE

Recorder builder source and license:
https://github.com/RPi-Distro/pi-gen/tree/ca8aeed0ae300c2a89f55ce9617d5f96a27e99e5
https://github.com/RPi-Distro/pi-gen/blob/ca8aeed0ae300c2a89f55ce9617d5f96a27e99e5/LICENSE

The patches change build-host emulation, required build packages and generic
first-boot state cleanup. They are modifications, not unchanged upstream files.
OSE modifications are GPL-3.0-or-later; retain the upstream BSD notice as well.

## Direct Python dependencies

pyproject.toml declares these separately installed packages. Licenses refer to
the upstream projects; inspect the exact resolved distribution before shipping
dependencies. The VMS source tree does not vendor their implementations.

- cryptography: Apache-2.0 OR BSD-3-Clause; binary wheels may include additional
  licensed components such as OpenSSL and require their supplied notices too.
  https://github.com/pyca/cryptography/blob/main/LICENSE
- PyYAML: MIT.
  https://github.com/yaml/pyyaml/blob/main/LICENSE
- Segno: BSD-3-Clause.
  https://github.com/heuer/segno/blob/master/LICENSE
- pywebpush: MPL-2.0. Its dependencies, including py-vapid and http-ece, have
  their own notices; do not treat this entry as licensing the whole dependency tree.
  https://github.com/web-push-libs/pywebpush/blob/main/LICENSE

No claim that a future resolved dependency set has been approved follows from
these links. Source publication and distributing dependency binaries are
separate scopes.

## External runtime and build tools

- Frigate source v0.18.0 uses MIT; the container also contains other components.
  No Frigate source, container or model is copied into this source tree.
  https://github.com/blakeblackshear/frigate/blob/v0.18.0/LICENSE
- FFmpeg, go2rtc, Caddy, Tailscale, operating-system packages, camera libraries
  and emulation/build helpers are external runtime/build integrations, not OSE
  software relicensed by this repository. Build options and bundled components
  can change the obligations of a resulting binary or image.
- References to Rockchip/TI SDKs, drivers, runtime libraries or firmware are
  research evidence, not included binaries or permission to redistribute them.

## Research and evidence

Research records preserve dated findings, report provenance, source URLs and
short vendor/specification excerpts. Quoted third-party portions remain under
their original terms and are excluded from the OSE grants. Retain attribution.
Raw supplied transcripts are retained privately, not distributed in this source
tree. Their summarized estimates remain unverified unless supported by a named
source review or measurement; removing a transcript does not verify its claims.
Historical conclusions do not override
the current requirements or establish performance, current stock or price.

The software and research tree contains no production identity keys, private
deployment configuration, bootable image, camera footage, SDK archive or model
weights. A separately published release attachment is a separate distribution
and needs its own review; source licensing does not approve it.
