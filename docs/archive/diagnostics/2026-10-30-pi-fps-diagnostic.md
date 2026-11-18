# Pilot FPS diagnostic — 2026-10-30

Camera labels are anonymized; physical test units and reused logical
identities are described separately. Installation locations, access/recovery
status and subscriber state are retained privately. Measurements and failures
remain dated observations, not new verification.

This is a read-only baseline from the legacy recorder, not production-hardware
qualification. No recorder settings, camera settings, or services were changed.

## Result

The main H.264 streams arriving at the recorder are already far below their
configured 15 FPS; saved clips are in the same range. No Frigate recording-rate
defect is demonstrated. Detection performance falls under higher CPU load. No
software-only correction is demonstrated; stop tuning this legacy hardware and
move performance work to devices that meet the product gates.

## Evidence

- `vms-recorder` and `vms-frigate` were active during the measurement window.
- The recorder is a four-core Raspberry Pi 4 Model B Rev 1.2. `hostapd` is
  inactive, `dnsmasq` is masked and inactive, and `wlan0` is down.
- Frigate was sampled twice for 60 seconds at 5-second intervals: 13 successful
  samples per run, no API failures. Only Camera B, Camera A and Camera C
  were bound to Frigate. A bench unit reported online in camera health, but had no
  Frigate binding and was not in this workload. The quieter tool-run window
  was 17:38:52-17:39:52 UTC.


- Camera B:
  - Frigate input FPS: 5.15
  - Processed FPS: 4.72
  - AI checks/s: 0.46
  - Skipped FPS: 0.43
- Camera A:
  - Frigate input FPS: 5.12
  - Processed FPS: 4.95
  - AI checks/s: 0.20
  - Skipped FPS: 0.22
- Camera C:
  - Frigate input FPS: 5.04
  - Processed FPS: 3.35
  - AI checks/s: 3.55
  - Skipped FPS: 1.69


This is the higher-load sample.
AI checks depend on motion/scene changes; they are not the processed-frame
rate. In the higher-load run, Camera C fell short of 5 processed FPS and
all cameras skipped frames above the product's under-1% gate. In the second,
quieter run, processed rates averaged 4.97, 5.11 and 5.11 FPS, with near-zero
skips. The brief low View/detection numbers therefore vary with workload; idle
AI-check rates are not a failure by themselves.

- Higher-load run: detector inference averaged 227.67 ms; host-wide CPU busy
  averaged 83.04% (about 17% idle); Frigate reported 83.97% full-system CPU.
  The quieter run averaged 53.64% host CPU busy but peaked at 85.25%, leaving
  only 14.75% headroom at that sample. Detector inference averaged 210.46 ms.
  Temperature averaged 64.64°C in the first run, and recorder health showed no
  current or historical throttle flags. Peak load misses the 30% CPU-headroom
  target; an eight-camera workload was not attempted.
- Two 8-second probes of each go2rtc RTSP stream at recorder ingress measured
  about 6.6–7.0 FPS (Camera B), 7.7–8.0 FPS (Camera A) and 5.7–6.7 FPS
  (Camera C). Wi-Fi health was reported healthy, with RSSI -41, -46 and
  -64 dBm and no reconnects. These readings do not measure the sensor before
  the camera/network handoff.
- Six recent H.264 recording segments per camera covered about 59–60 seconds.
  Packet-count FPS was Camera B 6.55, Camera A 7.23 and Camera C 6.37.
  One segment per camera was also counted as decoded frames; packet and frame
  counts matched. These saved rates are in the same band as the ingress probes.
  The two measurements were not time-aligned, so they do not prove frame-for-
  frame continuity into each file.
- Camera health reports a configured 1920×1080 H.264, 15 FPS profile; the
  camera agent supplies that value from configuration, not actual source
  throughput. 1920×1080 is about 2 MP, below the 5 MP product minimum.
- Browser View FPS was not measured. It is client-side and cannot be inferred
  from Frigate or saved-video rates. Saved segment presence was confirmed, but
  playback was not tested.

## Gate and next step

- **Pass:** active portal/Frigate, live Frigate stats,
  recent H.264 segments, and the no-hotspot guard.
- **Fail:** camera streams arrive at 5.7–8.0 FPS versus their 15 FPS profile;
  detection dropped frames during the higher-load sample; peak CPU headroom was
  below 30%; cameras are about 2 MP, not 5 MP.
- **Not tested:** sensor FPS before the network, browser View FPS, saved-video
  playback, four-camera load, and eight-camera capacity.

The main-stream shortfall is present at recorder ingress. The precise upstream
cause (camera capture/encoder versus transport) is not isolated; Wi-Fi health
is good, but that does not rule transport out. No recorder-side loss is
demonstrated. Detection backpressure is activity-dependent on this Pi. Do not
lower requirements or tune legacy hardware; repeat full acceptance on
qualified production devices.

## Reproduction

From the source checkout, stream the read-only tool to the Frigate container.
Replace RECORDER with an authorized SSH alias and use that installation's
configured stream names; the names below are examples:

    ssh RECORDER 'sudo -n docker exec -i vms-frigate python3 - --frigate-url http://127.0.0.1:5000/api/stats --duration 60 --interval 5 --source-stream camera_b=rtsp://127.0.0.1:8554/camera_b --source-stream camera_a=rtsp://127.0.0.1:8554/camera_a --source-stream camera_c=rtsp://127.0.0.1:8554/camera_c' < tools/fps_diagnostic.py

Add one `--recording /media/frigate/recordings/<date>/<hour>/<camera>/<segment>.mp4`
per clip and `--ffprobe /usr/lib/ffmpeg/7.0/bin/ffprobe` to count decoded frames.
The tool explicitly marks sensor-before-network FPS and browser View FPS as
unmeasured.
