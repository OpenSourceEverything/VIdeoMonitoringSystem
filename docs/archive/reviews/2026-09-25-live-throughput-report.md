> Historical reference. [Current documentation](../../README.md) takes precedence.

# OSE Cameras live-video throughput report

Camera labels are anonymized; physical test units and reused logical
identities are described separately. Installation locations, access/recovery
status and subscriber state are retained privately. Measurements and failures
remain dated observations, not new verification.

Date: 2026-09-25  
Status: measured pilot evidence, not a production claim  
Client: Samsung SM-G970U, Android 12, Brave/Chromium 153  
Recorder: Raspberry Pi 4, 4 GB RAM, Frigate 0.18, go2rtc, Tailscale

## Product architecture under test

```text
Legacy camera: 1280x720 MJPEG at about 10 FPS
  -> go2rtc FFmpeg input: software H.264, superfast/zerolatency, GOP 50
  -> Frigate: 5 FPS person detection and event recording
  -> OSE authenticated SDP endpoint
  -> go2rtc WebRTC media on TCP/UDP 8555
  -> Tailscale UDP
  -> Android browser video element
```

The OSE portal, camera media, event routes, and signaling require the owner
session. Frigate and go2rtc management APIs remain loopback-only. Tailscale
Serve is private to the owner's tailnet; public Funnel is disabled.

## Fault found and correction

The original Android symptom was a visibly choppy 0.5--0.8 FPS feed. Browser
instrumentation proved this was OSE's 1.5-second JPEG fallback, not a slow
WebRTC stream.

The browser received a valid SDP answer, but ICE stayed in `checking`. OSE's
generated go2rtc candidate list contained literal
`{FRIGATE_OSE_WEBRTC_CANDIDATE_*}` values. Frigate expands that syntax in
go2rtc stream URLs but passed these candidate entries through unchanged.
go2rtc ignored them and advertised a public STUN-derived address that the phone
could not reach.

Product commit `122cc83` now writes the already validated LAN and Tailscale
addresses directly into the private generated go2rtc YAML. TCP and UDP port
8555 remain mapped from the Frigate container. The product suite passed all
425 tests, the change was deployed, and runtime configuration showed both
private candidates.

## Measured successful remote session

The phone was using mobile data plus Tailscale, not home Wi-Fi. Chrome DevTools
Protocol instrumentation and `RTCPeerConnection.getStats()` measured:


- Signaling response:
  - Result: HTTP 200 SDP
- ICE connected:
  - Result: about 0.90 s after page navigation
- Peer connection connected:
  - Result: about 1.06 s after page navigation
- Selected path:
  - Result: Tailscale address, UDP 8555
- Video:
  - Result: H.264, 1280x720
- Browser-reported rate:
  - Result: 10 FPS
- 10-second decoded cadence:
  - Result: 100 frames / 10.005 s = 9.995 FPS
- RTP payload throughput:
  - Result: 2.598 Mbit/s
- RTP packet rate:
  - Result: 292 packets/s
- Packet loss during sample:
  - Result: 0
- Decoder frame drops during sample:
  - Result: 0
- Browser jitter:
  - Result: 31--32 ms
- Candidate-pair RTT:
  - Result: 57--68 ms
- Browser bandwidth estimate:
  - Result: 2.30--2.42 Mbit/s


The OSE UI displayed `WebRTC`, `Connected`, and `10.0 fps / 1 dropped`. The
single UI presentation miss is not a decoder drop: WebRTC stats reported zero
decoded-frame drops. After roughly 140 seconds, cumulative browser stats still
showed 1,461 decoded frames, zero lost packets, and zero decoder drops.

## Recorder load during that session


- Frigate container CPU:
  - Result: 111% of one CPU core equivalent
- Host load average:
  - Result: 3.40 / 2.68 / 2.40
- Host RAM used / available:
  - Result: 1.1 GiB / 2.6 GiB
- Swap used:
  - Result: 0
- SoC temperature:
  - Result: 64.7 C
- Throttling flags:
  - Result: `0x0` (none)
- Frigate camera/process FPS:
  - Result: 5.1 / 5.1
- Frigate skipped FPS:
  - Result: 0.0
- Recording volume free:
  - Result: about 875 GB decimal


The container's cgroup memory counter incorrectly reported zero, so host
available memory is the usable observation. This is one camera, one remote live
viewer, software MJPEG-to-H.264 conversion, and CPU detection. It is not a
four-camera capacity result.

## Two-camera scaling pass

Camera A was then bound to the same managed Frigate engine as Camera B.
Both cameras recorded, detected, and served remote WebRTC simultaneously. On
mobile data plus Tailscale, both streams rendered 1280x720 H.264 at 9.987 FPS
with zero RTP loss and zero decoder drops:


- Navigation to first decoded frame:
  - Camera B: 4.777 s
  - Camera A: 2.551 s
- ICE connected:
  - Camera B: 1.200 s
  - Camera A: 1.217 s
- RTP payload throughput:
  - Camera B: 2.883 Mbit/s
  - Camera A: 3.137 Mbit/s
- Jitter:
  - Camera B: 51 ms
  - Camera A: 51 ms
- Candidate-pair RTT:
  - Camera B: 63 ms
  - Camera A: 52 ms


Combined payload was about 6.02 Mbit/s. The media remained intact, but neither
sample met the proposed two-second first-frame gate.

The recorder had little scaling headroom. A 15-second cgroup sample with two
cameras and no live viewers averaged 189.2% CPU. During simultaneous live
viewing, instantaneous Frigate container samples ranged from about 243% to
311%; temperature reached 75.9 C without thermal-throttling flags. Detection
was stable at five input/processed FPS while idle, but skipped approximately
0.7 to 3.0 FPS during some activity/startup samples.

Process attribution identified the dominant cost:

- two legacy MJPEG-to-H.264 FFmpeg processes used about 67% and 63% CPU;
- Frigate record/detect FFmpeg processes used about 25% and 24%;
- CPU object detection used about 22%; and
- go2rtc forwarding used about 12%.

OSE's old capture worker was also opening cameras already managed by Frigate.
Product commit `5c9321a` removes that duplicate capture path. Managed cameras
now remain available to the portal while only Frigate records them. This fixed
a real product defect, but it does not remove the two permanent software H.264
encoders inside the legacy MJPEG adapter.

The evidence rejects the legacy MJPEG camera path as the production four-camera
design. It does **not** yet reject the Pi 4 recorder: the next controlled test
must remove software camera transcoding by supplying native H.264.

## Current product state versus a Ring-class experience

What is now credible:

- Camera B and Camera A remote live video both work at 720p10 through
  Tailscale with no measured loss or decoder drops.
- Person detection, event recording, exact-event links, Android Web Push, and
  recorded-event playback work through the managed Frigate path.
- Events have five seconds of pre-capture and post-capture, default seven-day
  local retention, permanent saved clips, download, and delete.
- Local operation does not require an OSE cloud service.

What is not yet Ring-class:

- Two managed cameras and two remote viewers have been measured, but not yet
  soaked. The Pi 4 plus legacy MJPEG software conversion is rejected as the
  four-camera retail architecture.
- First-frame samples of 2.55 and 4.78 seconds miss the proposed product gate;
  the causes have not yet been separated into page, signaling, keyframe, and
  decoder stages.
- Alert evidence is limited: working samples reached the push provider roughly
  6--13 seconds after event start, but phone-display and tap-to-first-frame
  latency are not yet instrumented end to end.
- Manual Tailscale reconnect restored tailnet ping in about 2.8 seconds. The
  tunnel remained connected across cellular-to-Wi-Fi and Wi-Fi-to-cellular
  changes, with tailnet reachability returning in about 6.2 and 5.6 seconds.
  Browser first-frame and alert delivery across those transitions remain
  unqualified.
- There is no accepted two-way audio, package detection, color night vision,
  or production-quality zones/sensitivity UX yet.
- Current output is 1280x720. Ring's current battery products advertise
  1440x1440 or 1920x1920 depending on model, and the 2K product recommends 10
  Mbit/s upload. Ring does not publish a universal live-view FPS target, so OSE
  should compare measured behavior rather than inventing a Ring FPS claim.

## Recommended acceptance targets

These are proposed OSE engineering gates, not claims about Ring internals:

- Live first frame: p95 under 2 seconds on LAN and under 3 seconds through
  Tailscale.
- Stable live cadence: at least 15 FPS on production direct-H.264 hardware;
  no silent snapshot fallback for an online, bound camera.
- Network: under 1% packet loss, no sustained decoder drops, and recovery from
  Wi-Fi/mobile transitions without owner shell work.
- Alerts: at least 20/20 initial walk-bys across day/night for each installed
  view, then a larger soak reporting miss and false-positive rates.
- Notification: record event start, detection, push submission, phone display,
  tap, and exact-event first frame; fix the worst p95 stage first.
- Multi-camera: four cameras plus two simultaneous viewers for 24 hours with
  recording and detection intact, no thermal throttling, and bounded CPU/RAM.
- Failure recovery: camera, Internet, VPN, storage, process, and recorder power
  interruption must recover without corrupting events or requiring physical
  camera access.

## Next experiment

Use the spare bench/debug Pi Zero W before changing either installed camera.
Its camera interface can use Raspberry Pi's V4L2 hardware H.264 encoder, so new
camera hardware is not yet required. Produce a native 1080p15 H.264 stream at a
bounded 2.5--4 Mbit/s, with repeated SPS/PPS and a one-to-two-second keyframe
interval. Let go2rtc copy that H.264 stream; no `libx264` process should exist
on the recorder for it.

Repeat the same ingest, detection, recording, remote WebRTC, CPU, temperature,
and first-frame measurements. Then scale the native-H.264 input to two and four
streams. The obsolete task is not retained as a current execution plan.
Maintained media design: ../../design.md#media-delivery; current acceptance:
../../requirements/requirements.md.

If four direct-H.264 streams pass, keep the Pi 4 recorder candidate and replace
the legacy camera image/path. If they do not pass after hardware-accelerated
detection is considered, upgrade the recorder. Do not optimize the legacy
MJPEG adapter further.

## Questions for external review

1. Is the observed 2.6 Mbit/s for 720p10 acceptable for this temporary
   `libx264 superfast` adapter, or is there one safe bounded bitrate/quality
   correction worth making before hardware migration?
2. Are the proposed p95 live and notification gates stringent enough for a
   credible consumer doorbell/camera alternative?
3. What failure and network-transition cases are missing from the next
   two-camera experiment?
4. Which direct-H.264 camera plus accelerated recorder architecture is the
   shortest path to four simultaneous cameras without discarding the current
   software?
5. What evidence should be required before claiming a Ring-comparable core
   loop rather than merely a working prototype?

Ring comparison references: [Battery Video Doorbell technical
specifications](https://ring.com/products/video-doorbell-v2) and [Battery
Doorbell 2K technical specifications](https://ring.com/products/battery-doorbell-2nd-gen).
Camera encoder references: [Raspberry Pi Picamera2
manual](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf) and
[Picamera2 source/documentation](https://github.com/raspberrypi/picamera2).
