# Product requirements

This is the single requirements document. Every saleable configuration must
meet each mandatory item below. Unknown evidence is not a pass. Qualify exact
models, hardware revisions, firmware, stream settings and complete landed cost.

## Product

- Recording, detection, viewing, playback, setup and storage work locally
  without a subscription, vendor account or required cloud service.
- Internet or remote-provider loss does not interrupt local use. The company
  does not store or relay customer video. Remote access and customer-owned
  gateways are optional and disclose their costs and dependencies.
- Publish source and build instructions. Disclose supported hardware,
  capacity, updates, service life, warranty, returns, certification and repair.
  Price must cover fulfillment, returns, warranty, support and maintenance.

### Camera and complete-kit price targets

- Controller price is not an eligibility requirement. There is no controller
  purchase-cost ceiling, selling-price ceiling or sourcing-to-selling-price
  ratio. Use documented prices only to rank controller options while market
  and RAM pricing are unsettled; do not derive a controller cap from kit targets.
- Standard outdoor camera, including mount and required power parts:
  maximum customer price $100; target $75-100.
- Premium night camera or doorbell: maximum customer price $150; target $100-150.
- One-camera system with useful recording storage: maximum $400; target $300-400.
- Two-camera system: maximum $500; target $400-500.
- Four-camera system: maximum $800; target $600-800.
- Eight-camera system: maximum $1,400; target $900-1,400.

Tax, installation, unusually long cables and optional extra storage are
separate. Supplier cost includes freight, duty and required accessories.

### Production sourcing cost

Camera procurement limits are separate from customer selling prices. Controller
prices are ranking information, not pass/fail gates. A retail offer is not
evidence of a repeatable production supply price.

- SRC-001: For cameras, target complete landed hardware cost at no more than
  40% of the intended customer selling price; the maximum is 50%. Use the actual intended
  selling price, not the higher product ceiling. This is a budgeting rule,
  not evidence that the remaining allowance is profit. Do not apply this ratio
  to controller eligibility.
- SRC-002: Landed hardware cost includes the exact installed configuration,
  required enclosure, cooling, PSU, ordinary cables, mounts/power accessories,
  allocated shared PoE hardware where applicable, inbound freight and duty.
  For a board-based controller include boot storage, assembly hardware and any
  drive-interface adapters. Count a shared component once in the complete kit.
  Recording storage remains a separately priced kit component, not boot storage.
- SRC-003: At the maximum camera selling prices, sourcing target/ceiling are
  $40/$50 for a standard camera and $60/$75 for a premium camera or doorbell.
  At a $75 standard-camera selling price they are $30/$37.50.
  Do not relabel an ordinary camera as premium solely to pass the cost gate.
  Rank controllers by documented complete landed cost when available; otherwise
  use the known priced subtotal, clearly labeled incomplete, with unpriced options
  last. Disclose missing parts and delivery charges. An unavailable offer is not
  a purchasable price and must not outrank a buyable option using an old low price.
- SRC-004: Each sourcing decision shall identify the buying URL, seller,
  exact SKU/revision, installed RAM/storage, condition, included parts, quantity,
  MOQ, observation/quote date, stock claim, shipping/duty and return/warranty
  terms. Unknown charges, ambiguous variants, old deals and unselected price
  floors are UNKNOWN, never zero cost or PASS. Preserve dated observations.
- SRC-005: Before production approval, price assembly/test labor, packaging,
  selling/payment fees, fulfillment, returns, warranty, support, software
  maintenance and margin separately. Demonstrate repeatable supply and a
  sustainable complete-kit cost. Do not impose a fixed controller price or
  margin percentage through this release decision. Passing a camera's
  hardware-cost ratio alone does not close it. A separately approved research
  sample may cost more, but cannot establish production economics or waive
  technical requirements.

## Controller (recorder)

- Finished, enclosed Linux appliance with documented revision, PSU, warranty
  and repair/spares path; at least 8 GB RAM and reliable boot storage. Select
  RAM against the complete simultaneous workload and peak-memory budget;
  neither 8 GB nor 16 GB alone establishes capacity. Workload, resource reserve,
  reliability and acceptance limits remain unchanged.
- A purchased single-board computer in an OSE-sourced or manufactured enclosure
  is allowed if the completed appliance meets every requirement. Qualify the
  exact board, boot storage, power, cooling, drive connection, assembly and
  maintenance path together. A bare SoC or board price is not a controller
  price; no custom PCB or firmware project is required merely by choosing this
  assembly route.
- FAB-001: The controller compute board shall have an available, revision-matched
  production package permitting OSE to initiate fabrication and assembly through
  an independent manufacturer: editable schematics and PCB layout; a BOM with
  exact parts and permitted substitutions; layer stackup and impedance rules;
  Gerbers or equivalent fabrication data, drills, placement and assembly drawings;
  and required programming, boot provisioning and production-test instructions.
  Files must carry an explicit license permitting commercial manufacture,
  redistribution and modification. Schematic PDFs, pictures and mechanical
  drawings alone do not satisfy this requirement.
  For a compute module and carrier, identify their files and rights separately;
  carrier-only files do not qualify the complete compute board. Establish a
  sourceable component BOM and the independent manufacturer's required inputs;
  file availability alone does not prove yield, cost or component supply.
  A purchasable preassembled version of that design is preferred. Missing files
  or rights are UNKNOWN, not PASS. This authorizes no fabrication or purchase.
- Maintained OS support for networking, hardware video decode and dedicated or
  integrated inference acceleration.
- Wired home-LAN Ethernet of at least 1 Gbps; 2.5 Gbps preferred. The controller
  joins the owner's router and never provides a Wi-Fi hotspot.
- Replaceable endurance-rated recording drive. A drive failure must not silently
  redirect video to boot storage; provide a documented restore path.

## Cameras

- Finished outdoor camera, continuous mains/DC or PoE, with mount and required
  power accessories. Support a mix of Wi-Fi, PoE and wired doorbell models.
- Dual-band 2.4/5 GHz Wi-Fi. A 2.4-only exception requires proven range and
  recovery at the installed location. WPA2-Personal minimum; WPA3 preferred
  when that exact camera and router support it. IP66 or better and a documented
  operating-temperature range.
- At least 5 MP at the required video setting and 15 sustained FPS; 8 MP and
  20–25 FPS preferred. Plain H.264 main and detection substreams must run at
  the same time. Selectable H.265 is an extra, not a substitute for H.264.
- Main-stream bitrate target 2–4 Mbps; substream 0.3–1 Mbps. Preserve image
  quality and size the system from measured rates. Detection substream target
  is 360p–720p.
- Local authenticated RTSP/ONVIF; configure resolution, FPS, bitrate and
  keyframe interval. Camera has a microphone and speaker for local two-way talk,
  at least 30 m IR, and usable moving-person facial detail at 5–10 m day/night.
  Require HDR/WDR at strongly backlit entrances.
- Local activation, network setup, credential changes, streams, talk, reset and
  firmware updates must work without a vendor app, account or cloud service.
  Qualify exact model/revision/firmware; battery/solar models must prove wake,
  pre-roll, event delivery, live view, recovery and advertised energy use.

H.265 generally saves bandwidth, but client support varies. Enable it only after
the recorder and supported clients pass recording, live view, playback, export
and capacity checks. A listing that says H.265 alone does not prove whether
plain H.264 is available; verify each stream.

## Video and detection

- Record the camera's native compressed main stream without routine
  transcoding; run detection on the lower-resolution stream.
- Support continuous and event-only recording, complete event intervals,
  distinct simultaneous events and playback/export on every supported client.
- Detection controls reflect supported camera capabilities. Alerts must not
  interrupt recording or viewing; bounded retries reuse a stable alert ID and
  never discard the local event.

## Storage

- Default retention is 7 days; allow 1, 3, 7, 14 or 30 days. Keep at least 20%
  free space in sizing. Verify drive identity, capacity and read/write access.
- Delete oldest unprotected recordings first. Saved clips are recorder-side
  copies outside normal retention and remain until explicitly deleted.
- Initialize only an explicitly selected empty drive. Reuse matching initialized
  drives; preserve unexpected existing data. Show used, free and total capacity.
- Plan usable recording TB as cameras × measured main-stream Mbps × 10.8 × days
  ÷ 0.8 ÷ 1000, plus audio and other overhead. Four 4-Mbps cameras for 7 days
  need about 1.51 TB; 2 TB is a planning size, not a guarantee.

## Home network and remote access

- Cameras join the owner's ordinary home Wi-Fi/mesh. The controller joins that
  same LAN, preferably over Ethernet. No recorder-hosted Wi-Fi or hotspot.
- Owner can change SSID, supported security, country and band without code
  changes; the access point chooses the channel. Use DHCP and stable camera
  identity independent of IP; static addresses and router reservations are not
  required.
- Camera network credentials are provisioned directly to the camera and stored
  in its protected credential store. Never place them in the controller, logs,
  reports or backups.
- A camera, router, access-point or address change recovers without re-imaging.
  One failed or moved camera does not interrupt others; recovery is bounded and
  has no reboot loop.
- Remote access is private and authenticated, needs no inbound router port and
  never exposes camera/internal endpoints publicly. Grids use substreams;
  constrained links lower quality and recover it automatically.

## Onboarding and identity

- One Add Camera flow claims, connects to home Wi-Fi, names and verifies a
  camera. QR or printed-code setup uses one-time authorization. Vendor QR codes
  are not OSE ownership claims.
- Setup, updates and recovery work without an SD-card swap. Browser setup never
  receives the home Wi-Fi password. Reject invalid, reused, expired or altered
  claims; an unclaimed nearby camera gets no network access.
- Prove permanent cryptographic identity independently of IP, MAC, access point,
  storage or board. Quarantine duplicate identities without replacing a working
  feed. Activate a camera only after identity, advancing streams and recorder
  frames pass.
- Interrupted setup resumes without duplicate registrations and restores the
  last working route. Reset securely removes ownership and network state;
  replacement revokes the old device while preserving its location/settings.

## Owner experience

- Fresh browser setup creates owner access, initializes storage and shows
  enrolled cameras. Ready means writable recording storage and at least one
  active camera. Ordinary use requires no IP, stream URL, shell, YAML, package
  install, vendor-engine UI or router rule.
- One Feeds interface provides live/history/playback, camera controls and
  diagnostics. Offline cameras remain visible as pending/unavailable without
  taking feed space. Camera and recorder health agree; distinguish power,
  network, sensor, video, recording and detection failures.
- Show available recording/detection FPS, skipped frames, inference latency,
  CPU/accelerator load, RAM, disk latency, network/power/temperature and
  first-frame time. Download versioned, redacted support reports; never include
  credentials, raw private paths or unredacted connection details.
## Security

- Authorize settings, changes, media and diagnostics. Commissioning is encrypted,
  authenticated, replay-resistant and bound to physical claim proof. Device
  credentials are distinct; never put credentials in URLs, arguments, source,
  logs, browser storage, reports or ordinary backups.
- Verify signed updates for authenticity, integrity and compatibility, with
  tested rollback. Reset, credential rotation and replacement revoke old access.
- Phone layout works at 360 CSS pixels, with accessible dialogs, focus and
  browser Back. Existing entry URLs and notification links keep working.

## Performance and capacity

| Measure | Minimum |
| --- | ---: |
| Simultaneous native main-stream recordings | 8 |
| Software camera capacity without UI redesign | 16 |
| Detection processing | 5 FPS per camera across 8 substreams (40 FPS total) |
| Steady skipped detection frames | under 1% |
| CPU and inference headroom at peak | 30% each |
| Full-load acceptance soak | 24 hours, including nearly full storage |
| Day/night person acceptance events | 20 each, end-to-end |
| LAN event first frame | at most 2 seconds |
| Cold start to advancing video | at most 120 seconds; target 60 |

No thermal throttling under supported load.

## Acceptance

Test simultaneous recording,
detection, grid/viewing, alerts, playback, export and retention. Verify exact
hardware and stream settings, fresh distinct recorder frames, complete event
playback, day/night image quality, supported clients, setup/reset/replacement,
storage failure, network/internet loss and recovery. Simulated cameras alone do
not establish product acceptance. Advertise only measured capacity.
