# Historical pilot observations - September to October 2026

Camera labels are anonymized; physical test units and reused logical
identities are described separately. Installation locations, access/recovery
status and subscriber state are retained privately. Measurements and failures
remain dated observations, not new verification.

Measured and reported evidence salvaged from obsolete task/decision documents.
This is not an intermediate requirements file, a design alternative, a work
queue or authorization to resume experiments. Old acceptance targets and
unexecuted checklists were not carried forward. Statements about implementation
and pending work describe their original dates, not current status.

[Current state](../../README.md), [requirements](../../requirements/requirements.md)
and [system design](../../design.md) take precedence. Later pilot measurements
are in the [2026-10-30 FPS report](2026-10-30-pi-fps-diagnostic.md).
Reported observations are not independently repeated measurements or production PASS.

## Implementation and first bench result

Original source: docs/archive/tasks/native-h264-camera-benchmark.md. Only the reported observation is retained.

The pilot.5 camera runtime and recorder adapter are implemented and pass the
complete host-side product suite. A real Zero W plus OV5647 bench camera was
flashed and provisioned through the factory web path on 2026-09-27. The shared
image preserved the existing `camera-002` identity, joined the recorder's
private network at a dynamic address, and passed every recorder-side check:
startup, identity, sensor, power, enrollment, network, recorder reachability,
JPEG snapshot, and continuous H.264 stream.

The camera uses Picamera2's hardware H.264 encoder at 1920x1080/15 FPS and
3 Mbit/s, serves a bounded authenticated Annex-B stream, captures JPEG
snapshots from a 640x360 low-resolution stream only when requested, and
advertises a schema-4 media capability. The recorder selected that capability
and handed the stream directly to go2rtc. Process attribution showed no
recorder-side `libx264` process for this camera; the two remaining `libx264`
processes belonged to the installed legacy MJPEG cameras. A Frigate-produced
snapshot from the relocated test unit returned 17.6 KB in 93 ms.

This was not a factory-new add-camera test. The card retained the already
enrolled `camera-002` / bench unit logical identity, so the result proves identity
preservation, reprovisioning, recovery, and native-media compatibility. It does
not prove that an owner can claim a previously unknown camera through the UI.
The fourth physical camera must start unclaimed, appear as a new camera, be
claimed and named through the owner interface without a shell, and reconnect
after reboot before new-camera onboarding is marked accepted.

This proves the native endpoint and capability path, but it does not yet pass
the 30-minute, remote-live, walk-by, restart, or multi-native-camera acceptance
steps below.

First boot also exposed a recoverable image issue: the first cloud-init
local/network pass failed and the guarded pilot reboot ran at about four
minutes. The second boot completed both stages, the VMS provisioning
transaction, secret cleanup, network recovery, and camera startup without
human repair. Recovery is good; a sellable image should not show a failed first
pass and still needs a clean first-boot acceptance run.

## 2026-09-28 repeat-card incident

Original source: docs/archive/tasks/native-h264-camera-benchmark.md. Only the reported observation is retained.

The verified 16 GB image and retained Camera 2 identity booted, joined the
recorder network, registered its dynamic route, and allowed authenticated
recovery access. Repeated recorder and first-boot JPEG requests then exposed an
unbounded `ThreadingHTTPServer`/snapshot failure: 320 `CLOSE_WAIT` sockets and
364 request threads accumulated behind one blocked Picamera2 capture.

The camera agent now caps live HTTP handlers at eight, rejects a second
snapshot while one is active, encodes the low-resolution YUV planes directly,
and uses the native H.264 endpoint for future first-boot readiness checks. All
458 host tests pass, and the live hotfix holds at roughly 11--14 tasks with no
`CLOSE_WAIT` growth.

The same physical camera currently has a separate hardware blocker. Both the
OSE agent and Raspberry Pi's own `rpicam-still` report `/dev/video0` buffer
queue I/O errors after a clean reboot, while `get_throttled` reports `0x0`.
That isolates the next action to a powered-off ribbon/module inspection, not an
SD-card reflash or another identity change. Do not resume media acceptance or
the failed first-boot transaction until the native utility captures a JPEG.

## Recorder scaling result

Original source: docs/archive/tasks/native-h264-camera-benchmark.md. Only the reported observation is retained.

The first mixed three-camera run is sufficient to reject unaccelerated CPU
detection on this Pi 4 as the 8-camera product baseline. With two legacy MJPEG
cameras plus the native-H.264 bench camera bound to Frigate:

- Native camera input:
  - Result: 1080p15 H.264, no recorder `libx264` source

- Frigate detection target:
  - Result: 5 FPS per camera

- Total measured detection:
  - Result: 3.6 FPS

- Processed FPS per camera:
  - Result: 0.4-0.5 FPS

- Skipped FPS per camera:
  - Result: 2.6-4.8 FPS

- CPU detector inference:
  - Result: 343 ms

- CPU detector process:
  - Result: about 134% CPU

- Recorder temperature:
  - Result: 79.3 C

- Thermal history:
  - Result: `throttled=0xe0000` (historic under-voltage/throttle flags)


Native H.264 successfully removes camera-source encoding work, but Frigate must
still decode frames for detection and the CPU object detector can process only
about three to five inferences per second under this load. This is far below
the tentative 8-camera target of 40 detection frames per second at 5 FPS each.
The relocated test unit's temporary Frigate binding was removed after the sample
to protect the two installed cameras' alert reliability. The camera remains
provisioned and available in OSE for naming and direct diagnostics.

## 2026-09-21/22 Frigate integration observations

Original source: docs/archive/decisions/2026-09-frigate-engine.md. Only the reported observation is retained.

On 2026-09-21, pinned Frigate 0.18.0 ran successfully in an ARM64 container on
the current 4 GB Raspberry Pi 4 recorder. The isolated proof accepted the
authenticated 1280x720 HTTP MJPEG Camera A source, transcoded it through
go2rtc to H.264, exposed its API on loopback only, and wrote playable recording
segments without interrupting the existing OSE service.

That test also rejected the present hardware/stream combination as a retail
baseline:

- one camera used roughly 147% to 334% container CPU under activity;
- camera FPS was about 2.1 and processing FPS about 1.6 against a target of 5;
- CPU detector inference was about 156 to 173 ms;
- MJPEG-to-H.264 transcoding alone averaged roughly 60% CPU; and
- the connection degraded and stalled during the run.

Four legacy MJPEG cameras plus CPU detection are therefore not a credible Pi 4
product. Frigate remains the selected integration direction, but the sellable
baseline should ingest H.264 directly and use qualified acceleration. An Intel
N100-class recorder/iGPU and supported accelerator/SBC combinations are
candidates for later measured hardware selection. The Pi 4 stays useful as the
legacy software/provisioning reference, not a promise about the final BOM.

On 2026-09-22 OSE replaced the pilot-only supervisor with a convergent appliance
adapter. It derives Frigate YAML and a separate mode-0600 credential environment
from OSE camera sources, bindings, and engine-neutral policy. Hostnames are
resolved to a single non-public address on the recorder because the container
does not have mDNS. Source changes trigger reconciliation immediately, and a
two-minute timer corrects address changes; the engine and portal restart only
when the derived state actually changes. A failed route caused an automatic
rollback during deployment, and the corrected cutover settled at 5.0 input and
processed FPS with no skipped frames. The old supervisor remains disabled as a
short-term rollback during soak.

## 2026-10-03 detect-ingest failure

Original source: docs/archive/tasks/driveway-home-lan-pilot.md. Only the reported observation is retained.

**Do not count this pilot as recording reliably.** `osecameras.local` resolves
and the portal serves its login page, but the last authenticated post-deploy
check showed **0 Frigate detect-path input FPS** for Camera A and Camera B.
The new on-demand camera check saw advancing frames from both cameras at the recorder.
That separates this sampled failure from a proven camera/Wi-Fi outage: video
reached the recorder application, then failed in Frigate's detect ingest path.
The legacy Pi MJPEG-to-H.264 conversion remains the leading, separately tested
suspect. This does not establish why every earlier event was missed.
At 09:18 UTC, Frigate was still at 0 detect input FPS for both cameras and
logged RTSP read timeouts. Its latest retained recording file was dated
2026-10-02;
retention is motion-only, so that file date is supporting evidence rather than
a standalone recording-continuity measurement. The separate record stream and
disk persistence were not measured by `camera_fps`. An encoder test that
remained stuck after its intended timeout was terminated by exact process ID;
the three recorder services remained active, but recording recovery was not
established.

## Retired bench unit sensor observation

Original source: docs/archive/tasks/driveway-home-lan-pilot.md. Only the reported observation is retained.

The powered-off reseat was completed and did not restore capture. Linux sees
the OV5647 path but reports repeated I2C read error `-5`, sensor-mode programming
failure, and CSI stream-start failure. This is now a component-isolation task,
not another software or recorder retry.

The boot's degraded state is downstream of the same failure: first-boot waits
for camera readiness, reports `camera readiness probe failed`, and the
provision/cleanup units retry behind that transaction. This is not evidence of
a separate SD-card, network, or provisioning defect. Leave the retry path
intact so a repaired physical camera can complete automatically.

The failed bench unit was retired after a ribbon reseat did not restore capture.
The hardware failure remains recorded; no successful repair is claimed.

## 2026-09-29 radio and duplicate-identity observations

Original source: docs/archive/tasks/driveway-home-lan-pilot.md. Only the reported observation is retained.

Stop the existing Pi Zero camera-network experiment here. Camera A and Camera B
remain a valid two-camera recorder proof: the recorder portal, private
camera AP/DHCP, route relay, Frigate, and reconciliation timer were active;
Frigate was healthy; both feeds produced changing snapshots at about 5 FPS
with zero recorder-reported skipped FPS in the latest sample.

The relocated physical test unit did not join the home LAN as `camera-003`. It later
associated with the recorder AP using a duplicate `camera-002`/bench unit identity,
requested the same address as the bench unit, and initially disconnected and reconnected.
After the original broken bench unit radio was isolated by exact MAC and the relocated
unit was forced through one clean reassociation, DHCP rejected the stale address and
assigned a fresh address. Authenticated health then reported camera ready, system
ready, healthy network, recorder reachable, and no power fault; two snapshots
changed and the native H.264 endpoint advanced.

The corrected link remained associated during the short clean measurement, but
it had only about -85 dBm at the camera, frequently negotiated 1--6.5 Mbit/s in
the camera-to-recorder direction, and accumulated 142 AP transmit failures in
194 seconds. That is functional edge connectivity, not acceptable product RF
margin. The duplicate identity also prevents treating this as a clean
onboarding or signed route-handoff proof.

## 2026-09/10 source and transcoding observations

Original source: docs/archive/tasks/driveway-home-lan-pilot.md. Only the reported observation is retained.

The latest recorder evidence does **not** isolate RSSI as the sole cause.
Camera A and Camera B each reached about 5 recorded/process FPS with zero skipped
FPS, but the input monitor counted 101 and 26 stalls respectively in the
preceding hour. Both legacy sources are MJPEG, so the recorder was running two
live `libx264` conversions at roughly 72--73% CPU each; whole-system CPU was
about 79% and load average was about 14. Memory and disk capacity were not the
limiting signals in that sample.

That means the visible chop is consistent with two compounding limits:

- the old cameras deliver only a low, sometimes stalled MJPEG source; and
- the Pi 4 must convert every source to H.264 before Frigate/browser playback.

Recorder `skipped_fps=0` means Frigate was not simply discarding detection
frames. The deployed camera agents do not publish usable RSSI, so these logs
cannot determine how much of each source stall began on Wi-Fi. The source now
records RSSI when a camera reports it, recorder input stalls/reconnects, source
codec, recorder transcoding, recording FPS, and skipped FPS, and presents a
plain likely-cause diagnosis. This diagnostic panel is deployed on the
recorder. Its `skipped_fps` value is Frigate's detection-processing backlog,
not proof that the H.264 recording lost frames; the portal labels the two
separately.


**2026-10-03 layer-isolation check:** Frigate reported 0 input FPS for both
Camera A and Camera B and 326 reconnects in the preceding hour. Both camera
control endpoints responded, and each camera delivered two advancing MJPEG
frames directly to the recorder in under 0.3 seconds. From inside the Frigate
container, the private route relay delivered media for both cameras. A bounded
ten-frame decode-only test and a software-H.264 encode test completed; the
same bounded test using the Pi `h264_v4l2m2m` encoder timed out after 12 seconds.
This *snapshot* isolates the current failure to the recorder's legacy
MJPEG-to-H.264 path, not to a proven Wi-Fi outage. It does not explain missed
events outside that measurement window. No camera firmware was changed.

## 2026-09-29 encoder mitigation observation

Original source: docs/archive/tasks/driveway-home-lan-pilot.md. Only the reported observation is retained.

The managed adapter now detects the Pi 4 `bcm2835-codec-encode` device and uses
go2rtc's `h264_v4l2m2m` path for MJPEG-only legacy cameras. It preserves direct
copy for native-H.264 cameras and falls back safely when the encoder is absent.
Both installed legacy feeds are running through `/dev/video11`.

- Per-camera MJPEG-to-H.264 process:
  - Before: about 42--45% CPU
  - After: about 16--17% CPU

- Recorder idle CPU in observed sample:
  - Before: about 1.6%
  - After: about 12.8%

- Fresh Camera A clip:
  - Before: older event samples were 1.3--3.3 effective FPS
  - After: 10.0 FPS over 22.0 seconds

- Fresh saved-segment cadence:
  - Before: about 10 FPS
  - After: about 10 FPS, maximum observed timestamp gap under 0.4 seconds


This proves a recorder-side bottleneck existed and the hardware-encoder change
materially reduced it. It does not repair the Pi Zero W radio or turn a
low-frame-rate MJPEG camera into product hardware. Camera B had no retained
post-change event clip in the measured window because retention is motion-only;
its post-change raw segment was approximately 10 FPS. A new walk-by event is
the remaining human-visible playback confirmation.

Frigate's CPU detector can still fall behind temporarily. Detection skips are
now reported as detection skips, not recording drops. An accelerated detector
is a separate recorder-capacity gate; it does not justify more work on the Zero
W camera platform.

## 2026-09-21/22 notification observations

Original source: docs/archive/tasks/motion-to-phone.md. Only the reported observation is retained.

Android Chrome Web Push enrollment and a recorder-triggered test notification are
implemented. On the private HTTPS recorder page, the owner can enable, test,
and disable notifications for that phone. The test opens the protected Events
page. This proves only the transport and phone permission path: there is no
motion detector, event-specific deep link, alert queue, or automatic motion
notification yet. The endpoint is restricted to Chrome/FCM in this first pilot;
other browsers need deliberate support and testing. Push delivery requires the
recorder's outbound Internet and the platform push service. It does not require
an OSE-operated backend.

The Pi 4 proof on 2026-09-21 established that Frigate can ingest the current
authenticated MJPEG source, record H.264, and expose its API. It also ruled out
Pi 4 plus legacy MJPEG transcoding and CPU detection as a four-camera retail
baseline. The Frigate observations above retain the measurements.

On 2026-09-22 the adapter, OSE Events read path, protected MP4 clip proxy,
event-specific deep link, and bounded Web Push delivery ledger were deployed to
the Pi 4 pilot. Frigate 0.18.0 is pinned behind recorder loopback port 15000;
no Frigate port is exposed to the LAN or tailnet. Camera B frames are reaching
Frigate, the legacy OSE recorder continues recording independently, and capture
cycles report the test alert path ready. A controlled walk-by on 2026-09-22
produced a person alert with an 84% peak detector score. Frigate closed the review
item and the Web Push
provider accepted exactly one delivery. The original event-to-delivery time was
about 80 seconds: most of the avoidable delay came from Frigate's default
40-second alert cutoff after activity stopped. The deployed pilot now uses a
10-second cutoff. Human confirmation that the alert appeared and opened the
exact protected clip is still pending and must not be claimed complete.
The event page now requests Frigate's full review clip rather than its
low-frame-rate preview, displays the review duration, and serves authenticated
MP4 byte ranges required for reliable Android browser playback and seeking.
Each card also proxies a protected detector thumbnail as its poster, and the
portal Content Security Policy explicitly permits same-origin video; without
that media rule Android Chrome correctly blocked the otherwise-valid clip.

## 2026-09-25 notification delay limitation

Original source: docs/archive/tasks/motion-to-phone.md. Only the reported observation is retained.

The owner observed that some otherwise-correct notifications appeared late.
Static inspection found a concrete queueing hazard, but the current pilot does
not retain enough stage timestamps to attribute those individual samples. Alert
delivery currently runs only after the legacy recording capture cycle. That
worker is scheduled two seconds after its previous run becomes inactive, but a
legacy source may hold the cycle for up to 30 seconds before the alert check.
This can add variable delay unrelated to Frigate detection or the phone push
service. It also makes the nominal two-second timer an inaccurate alert-latency
bound.
