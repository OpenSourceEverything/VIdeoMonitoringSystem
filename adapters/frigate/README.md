# Frigate appliance adapter

Frigate is a private, replaceable media engine behind the OSE application. It
is not a customer-facing service and its unauthenticated internal API must bind
only to loopback or an isolated container network.

The `pilot` files prove one real legacy MJPEG camera end to end. They are not a
retail performance baseline: the Pi 4 must transcode MJPEG and uses Frigate's
test-only CPU detector. The legacy OSE recorder remains active during this
pilot.

The pinned container exposes only Frigate port 5000 as recorder loopback port
15000. In the legacy direct-source mode, camera credentials enter through the
private Docker environment file; they are never committed to this directory.
FFmpeg output is suppressed in that mode because its HTTP input errors
otherwise echo credential-bearing source URLs. OSE proxies normalized review
metadata and bounded previews through owner authentication.

The managed adapter now includes that credential-injecting path. The route
relay binds only to Docker's private bridge gateway, admits only the exact
container subnet, and supplies stable credential-free URLs to go2rtc. Camera
authorization remains in the recorder process and is sent upstream as a
header; it is not placed in go2rtc API queries, generated YAML, process
arguments, or relay state. Existing schema-1 engine state does not enable the
relay automatically: migration remains an explicit bench-qualified operation.

On a Pi 4 with the exact `bcm2835-codec-encode` device, the installer maps
`/dev/video11` into the private container and selects go2rtc's
`h264_v4l2m2m` output for legacy MJPEG sources. Other hosts omit that mapping;
native-H.264 sources always remain direct-copy inputs. Container console logs
rotate at 10 MiB with three files so a noisy media failure cannot consume the
recorder filesystem. The system journal has its separate appliance cap.

The pilot environment also supplies `FRIGATE_WEBRTC_LAN_CANDIDATE` and
`FRIGATE_WEBRTC_TAILSCALE_CANDIDATE`, each in `address:8555` form. TCP and UDP
8555 carry encrypted WebRTC media; they do not expose the Frigate HTTP API.
Browser SDP is accepted only by the authenticated OSE camera-slot endpoint and
is proxied to loopback. Retail setup must derive candidates rather than ask an
owner to find or type IP addresses. A customer-owned public gateway requires a
separate relayed-media/TURN decision; its reverse HTTP tunnel is not a UDP
relay.
