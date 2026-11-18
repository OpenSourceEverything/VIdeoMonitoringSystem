> Historical reference. [Current documentation](../../README.md) takes precedence.

# OSE Cameras external architecture review packet

Camera labels are anonymized; physical test units and reused logical
identities are described separately. Installation locations, access/recovery
status and subscriber state are retained privately. Measurements and failures
remain dated observations, not new verification.

Date: 2026-09-23

## Review request

Challenge the architecture and execution order below for a sellable,
local-first, open-source home camera product. Treat this packet as working
evidence, not as a desired conclusion. Identify hidden security, reliability,
licensing, performance, operations, accessibility, and support risks. Separate
problems that can be solved in software from evidence that truly requires a
hardware change.

Please answer:

1. Is authenticated Frigate HLS VOD the right replacement for OSE's buffered
   MP4 path, and is any custom media behavior still unnecessary?
2. Is the proposed one-shell UI compatible with exact notification deep links,
   browser history, progressive enhancement, and accessible phone use?
3. Is QR/share/PWA installation the right no-SaaS answer to Tailscale's long but
   certificate-valid private hostname? What safer low-friction option is missed?
4. Is the OSE/Frigate boundary narrow enough to permit engine upgrades or
   replacement without recreating a media engine?
5. Which test should gate adding the second camera, choosing recorder hardware,
   and selling a first kit?
6. What work is incorrectly ordered, missing, or unnecessarily custom?

## Product promise and non-goals

OSE Cameras is intended to be a consumer appliance, not a server project for
the owner. The owner plugs in a recorder and supported cameras, names them,
sees feeds, receives useful activity alerts, and plays recordings. Recording
continues locally without Internet service.

- No mandatory OSE account, subscription, backend, video relay, DNS service, or
  notification server.
- Optional remote access is either the owner's private Tailscale network or a
  customer-owned DigitalOcean gateway.
- The customer sees OSE setup, authentication, UI, PWA, diagnostics, and update
  behavior. The customer does not see Frigate, YAML, Docker, IP addresses,
  camera credentials, or shell commands.
- Current Raspberry Pi hardware is a software and migration fixture. The retail
  camera and recorder BOM is intentionally not selected yet.
- The product is not trying to reproduce every Ring feature before it proves a
  dependable motion-to-phone loop.

The first competitive outcome is:

```text
motion -> one durable event -> one useful phone alert
       -> tap -> the exact authenticated, playable clip
```

## Current architecture

```text
supported cameras
       |
       v
private, version-pinned Frigate media plane
       |
       v
OSE application and engine-neutral adapter
       |
       +-- owner-authenticated Feeds / events / settings / diagnostics
       +-- Web Push notification ledger and exact-event deep links
       +-- local storage and appliance policy
       +-- optional Tailscale Serve or customer-owned gateway
```

Frigate owns stream ingest, decode, motion/object detection, review/event
lifecycle, recording mechanics, retention mechanics, and WebRTC/restream
capability. OSE owns stable camera identity, owner authentication, policy,
provisioning, remote access, notifications, packaging, and the only customer
interface. Frigate's unauthenticated HTTP API is loopback-only. OSE does not
read its database or media layout and namespaces engine event IDs.

## Proven on the live legacy pilot

- A Pi 4 recorder and original Pi Zero W cameras were used in a small local
  pilot. Local and authenticated Tailscale HTTPS access were exercised.
- Owner login, Android browser/PWA use, storage adoption, local recording,
  camera naming, active/inactive recording, diagnostics, and per-camera plus
  global alert controls work.
- Pinned Frigate 0.18.0 is hidden behind OSE. OSE generates its configuration
  and separate credential environment from stable camera bindings and
  engine-neutral policy, resolves private camera addresses, and reconciles
  changes without owner YAML or container work.
- Camera B ingest settles near 5 input and processed frames per second with
  zero skipped frames after correcting false source timestamps. A contour
  filter and scoped vegetation mask were configured for the test scene.
- Real Camera B activity creates Frigate review events and OSE Web Push
  alerts. A 2026-09-23 user report said alerts appeared to work; that report is
  not an end-to-end latency measurement.
- One alert is sent when Frigate first confirms an active alert. The same event
  ID and delivery ledger prevent a second completion notification. Recording
  is independent of push delivery.
- Protected event thumbnails, MP4 compatibility clips, Android byte-range
  responses, and notification links exist. A tested change now adds narrow
  authenticated HLS VOD manifests/segments. Live tiles attempt authenticated
  WebRTC and retain protected snapshots as a fallback.
- The selected camera's Diagnostics dropdown exposes the browser-selected live
  path, connection state, displayed FPS, and dropped frames without telemetry.
- The recording volume was deliberately reset after stopping its writers. Old
  media and stale Frigate database references were removed while product state,
  camera credentials, and backups were preserved. Fresh recording and healthy
  Camera B detection resumed.
- The next tested slice sets a 7-day default, offers 1/3/7/14/30-day retention,
  and maps OSE Save Clip/Download/Delete to Frigate exports and review actions.
  Saved exports are outside rolling retention; no custom OSE storage engine was
  added.
- All 423 automated tests pass with the saved-clip slice.

This is a useful vertical slice, not release qualification. Only Camera B is
currently bound to the managed Frigate event engine. Formal 20-pass detection
reliability, four-camera operation, power/storage/network fault injection,
signed update/rollback, and an outside-household trial remain open.

## Current performance evidence and leading hypotheses

### Recorded event playback

The browser requests an authenticated OSE MP4 endpoint. The OSE Frigate adapter
currently downloads the complete review clip, up to 64 MB, into memory. The GUI
then interprets the browser's `Range` header and returns a byte slice. Each new
range can repeat the full upstream fetch. This creates avoidable time-to-first-
byte, memory, loopback, and CPU cost and is the leading software bottleneck for
slow event playback.

The first external review correctly rejected a custom OSE MP4 seek engine.
Frigate 0.18 was then verified on the live loopback-only pilot: its nginx-vod
route returns a master playlist, a media playlist, one initialization MP4, and
bounded M4S segments. An OSE event is a Frigate review rather than an object
event, so the adapter resolves the review to its authorized camera and time
range and constructs that private VOD path itself.

The implemented next slice exposes only validated, rewritten, same-origin OSE
manifest/segment routes. It rejects arbitrary URLs, paths, camera names, time
ranges, traversal, active reviews, malformed manifests, and oversized
resources. The browser prefers HLS and retains MP4 only as compatibility. The
HLS route still needs live Android validation, ten-event measurement, and
possibly backpressured per-segment delivery after measurement.

### Live video

Live and recorded playback are separate paths. The tile attempts WebRTC through
OSE-authenticated SDP signaling. If negotiation fails, it refreshes a protected
JPEG every 1.5 seconds, which is inherently choppy. Visible diagnostics now
make the chosen path measurable. If WebRTC is connected but choppy, the next
isolation points are legacy MJPEG capture, CPU H.264 transcode, go2rtc,
networking, phone decode, and render cadence.

The corrected follow-on reuses go2rtc MSE between direct WebRTC and degraded
snapshots rather than implementing another live protocol. The current WSGI
portal cannot proxy WebSockets, so this requires a deliberate narrow
authenticated transport/server integration.

The existing Pi 4 plus four legacy MJPEG transcodes and CPU detection is already
rejected as the retail baseline. Direct-H.264 cameras and qualified accelerated
recorders are the likely commercial direction. That broader conclusion should
not be misused to skip the evident event-proxy fix.

## Proposed next sequence

1. **Media performance:** validate and benchmark the authenticated HLS VOD
   slice, then collect visible LAN/Tailscale live-path evidence and reuse go2rtc
   MSE before degraded snapshots. Do not disturb working detection and alerts.
2. **Single-surface UI:** make Feeds the one visual shell. A selected camera
   owns live, events, recording/alerts, and diagnostics. Global settings open in
   a compact panel. Preserve legacy route entry and exact event deep links.
3. **Memorable remote entry:** standardize Open/QR/Copy/Share/install behavior.
   Tailscale's valid HTTPS URL remains the full tailnet name but is hidden behind
   an **OSE Cameras** home-screen icon. A customer-owned gateway may use the
   customer's memorable domain.
4. **Repeatability and parity:** package the managed Frigate adapter into the
   signed appliance update/rollback path, then bring a second real camera
   through it. Every visible Events action must either work or truthfully report
   that the capability is unavailable.
5. **Release evidence:** direct-H.264 and accelerated-recorder tests, sustained
   four-camera recording/detection/playback, retention/full-storage and
   power/network/camera fault recovery, security/license review, 30-day outside
   household trial, then BOM selection.

The old task contracts have been consolidated. Maintained design sections:

- Media delivery: ../../design.md#media-delivery
- Owner interface: ../../design.md#owner-interface
- Remote entry: ../../design.md#remote-access
- Notifications: ../../design.md#notifications

## Important design constraints

- Do not build another OSE motion detector, event lifecycle/deduplicator,
  retention engine, timeline store, or general restreamer while the hidden
  Frigate approach remains viable.
- Customer choices express intent: Sensitivity, Person/Vehicle/Any motion,
  Activity and Ignore zones, alert cooldown, schedule, and Data
  Saver/Balanced/Best. Raw detector FPS, confidence, contour, bitrate,
  resolution, and keyframe values stay under Advanced.
- Remote loss may stop remote view and push, never local recording or stored
  playback.
- Browser deep links contain no credentials or private camera metadata. Owner
  authentication remains required locally and remotely.
- Camera names are presentation. Stable cryptographic/product identity is
  separate from addresses, boards, and owner labels.
- No paid-release claim is based on this one household or the passing unit
  suite alone.

## Known unresolved risks

- The original MP4 compatibility route remains fully byte-buffered. HLS now
  bounds work per requested segment, but Android/native-HLS behavior,
  non-native-HLS support, backpressure, and ten-event performance remain open.
- Android live path and actual displayed cadence have not yet been recorded on
  both LAN and Tailscale despite visible diagnostics being available.
- Only one camera has managed Frigate events; the independent legacy capture
  worker still runs during migration.
- The customer-owned gateway still needs a deliberate TURN or other relay
  decision before remote WebRTC can be promised through its HTTP reverse
  tunnel.
- Signed appliance update and automatic rollback are not yet the normal pilot
  deployment path.
- Direct H.264, acceleration, four-camera thermal/load/storage behavior, and
  final commercial hardware are unqualified.
- Zero 2 W physical compatibility, product recovery, licensing/redistribution,
  supply, certification, enclosure, power, and landed cost gates remain open.

## Desired review output

Return:

1. a go/change/stop judgment on the architecture;
2. the five highest product risks, ordered by likelihood times impact;
3. a corrected next-ten-work-days sequence;
4. any component OSE should adopt instead of building;
5. minimum evidence required before choosing hardware and before accepting a
   paying pilot; and
6. concrete objections to the task acceptance criteria, not a generic startup
   checklist.
