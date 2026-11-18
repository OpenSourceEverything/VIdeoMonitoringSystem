# System design

This is the single current design document. It consolidates architecture,
hardware/BOM, onboarding, storage, media, UI, diagnostics, alerts, security,
provisioning and remote access without changing the product requirements.

[Product requirements](requirements/requirements.md) own mandatory limits.
[Current state](README.md) owns implementation status and next actions.
Controller offers are in research/candidates/controller.md; camera offers are
in research/candidates/camera.md. Their matching .json files own configurations and
[role-specific sources](research/README.md) retain the supporting research.
Described software, proposals and historical pilot
observations are not production-hardware approval or measured capacity.

Only dated research, reviews and measured evidence remain in
[Archive](archive/README.md). Do not keep intermediate requirements or competing
design snapshots in the working documentation.

<a id="contents"></a>

## Contents

- [Architecture](#architecture)
- [Product strategy and public-benefit mission](#product-strategy)
- [Hardware and BOM](#hardware-and-bom)
- [Phase 2: four property cameras](#four-camera-deployment)
- [Camera onboarding design](#camera-onboarding)
- [Provisioning](#provisioning)
- [Camera route handoff design](#camera-route-handoff)
- [Event storage design](#event-storage)
- [Media delivery design](#media-delivery)
- [Owner interface design](#owner-interface)
- [Central diagnostics](#diagnostics)
- [Notifications](#notifications)
- [Alert Delivery And Integrations](#integrations)
- [Security](#security)
- [Remote Access](#remote-access)

<a id="architecture"></a>

## Architecture

Use one local, recorder-led pipeline. Camera-side detection can supplement it
later; a cloud processing path is not being built in parallel.

```text
camera main H.264 -----------------> recording disk -> event playback/export
camera substream -> hardware decode -> motion/AI -> events -> alert worker
                                                            |
browser <-> application authentication, controls, diagnostics +-> push service
```

<a id="architecture-components"></a>

### Components

- Finished camera:
  - Responsibility: Native compressed main/substream video, local controls and device authentication

- Frigate/go2rtc:
  - Responsibility: Private media ingest, recording, detection, event lifecycle, retention and live transports

- Application:
  - Responsibility: Setup, camera identity, authentication, policy, UI, notifications and redacted diagnostics

- Linux adapters:
  - Responsibility: Hardware acceleration, storage attestation, service supervision and network integration


The application uses a narrow media-engine API, not its database or filesystem
layout. Internal management ports remain private. The browser never receives
upstream credentials or chooses an arbitrary media URL.

<a id="architecture-recording-and-detection"></a>

### Recording and detection

Copy/remux main-stream video to disk without routine re-encoding. Decode only
the smaller detection stream. GPU/NPU inference and video decode are separate
workloads; measure both on the selected recorder. Legacy Pi MJPEG conversion
is an isolated compatibility adapter, not the new-camera architecture.

<a id="architecture-network-and-identity"></a>

### Network and identity

Wi-Fi and PoE cameras share the home LAN; the recorder uses wired Ethernet.
Permanent authenticated device identity is separate from IP address or hostname.
The [route handoff](#camera-route-handoff) verifies advancing video before
publishing a changed endpoint. [Add Camera](#camera-onboarding) owns setup;
retail models need an actual qualified local driver, not just an RTSP checkbox.

<a id="architecture-storage-and-recovery"></a>

### Storage and recovery

Use the paired, attested recording volume. Missing or unhealthy storage stops
recording rather than redirecting it to the boot disk. The supervised engine
is restarted when safe even if configuration is unchanged. Failures remain
visible in history and saved clips. See [event storage](#event-storage).

<a id="architecture-viewing-and-alerts"></a>

### Viewing and alerts

[Media delivery](#media-delivery) owns authenticated live video, bounded HLS
playback and degraded fallbacks. [Alerts](#notifications) run independently
of recording. [Remote access](#remote-access) is optional and private; local
recording does not depend on it.

Capacity and release gates belong in [requirements](requirements/requirements.md).
Historical measurements remain in the [research archive](archive/README.md).

<a id="product-strategy"></a>

## Product strategy and public-benefit mission

Status: durable direction. Market prices are dated evidence, not permanent
product claims.

<a id="product-strategy-mission"></a>

### Mission

OSE Cameras exists to make dependable home security capability understandable,
obtainable, repairable, and owner-controlled. The company should disseminate
the ability to build and operate the system, not create dependence on OSE.

That means:

- source code, build instructions, protocols, and self-hosting paths remain
  publicly available;
- recording and detection continue locally without an OSE account, cloud, or
  subscription;
- customers can buy convenience--tested hardware, assembly, updates, warranty,
  and support--without buying artificial restrictions;
- standard cameras, storage, networking, and media mechanisms are preferred
  over lock-in; and
- revenue must sustain safe engineering and support, but recurring rent on
  customer-owned recordings is not the business model.

Open source does not mean unsupported hardware, unsafe defaults, or promises
that every device works. OSE can sell a small, rigorously qualified product
while publishing the full path to build, inspect, modify, and repair it.

<a id="product-strategy-transparent-selling-standard"></a>

### Transparent selling standard

OSE should help an owner make the right decision even when that decision is to
buy something else. Every public comparison must:

- show current competitor hardware prices beside one-, three-, and five-year
  cost, with the price date and direct source;
- distinguish sale price from regular price and required cost from optional
  service;
- say plainly when products are not equivalent--for example, eight 1080p
  analog cameras and a DVR are not eight independently addressable IP cameras,
  while a cloud camera can still have a materially better phone experience;
- disclose what OSE costs more, does worse, or has not yet proved, as visibly
  as its advantages;
- explain who should buy the cheaper or simpler competing product;
- separate verified facts, vendor claims, OSE measurements, and estimates; and
- keep a dated evidence page behind every material comparison so old prices or
  features can be corrected without rewriting the mission.

The sales claim is not “OSE always costs less.” It is: **here is the whole
market, here is what each path actually buys, and here is why OSE exists.** The
company earns trust by giving up sales that would require hiding a better fit
from the customer.

<a id="product-strategy-competitive-position"></a>

### Competitive position

The product is not simply a cheaper camera. It is a complete local recording
system with optional private remote access. Compare like with like:

- camera endpoints;
- recorder and usable storage;
- continuous or event recording and retained history;
- alerts, playback, remote viewing, and recovery;
- required subscriptions over one, three, and five years; and
- what remains usable if the vendor or Internet connection disappears.

Ring can sell a camera below OSE's target camera price and periodically cut it
further. On 2026-09-27 its official US site showed the 1080p Outdoor Cam at
[$39.99 sale / $79.99 regular](https://ring.com/outdoor-security-cameras).
The same vendor listed recorded event history under Ring Multi at
[$99.99 per year](https://ring.com/plans). Four sale-price cameras therefore
cost about $160 at checkout, about $460 with three years of Multi, and about
$660 with five years, before tax and accessories.

That comparison does not make Ring bad or OSE automatically better. It means
OSE cannot rely on “no subscription” as a slogan or pretend to beat every
promotion. OSE must make the ownership difference concrete: customer-owned
history, continued local operation, open implementation, repairability, and no
mandatory annual video bill.

Wyze is the low-price local-storage benchmark. Its official store advertised a
[$59.98 Cam v4 plus 32 GB card bundle](https://www.wyze.com/products/wyze-cam-v4-32gb-microsd-card)
with 24/7 local recording when checked. It is not thereby an OSE production
camera: exact standards-based streaming, secure OSE onboarding, firmware,
supply, and recovery still have to pass the same contract.

Packaged Reolink and other conventional NVR systems are the more relevant
architecture benchmark because recorder and disk are included. Their higher
checkout price is not directly comparable to endpoint-only camera pricing.

<a id="product-strategy-price-and-package-shape"></a>

### Price and package shape

The current target is an expandable system, not eight cameras for $300--$500:

- One-camera starter:
  - Target owner price: $300--$400
  - Purpose: Lowest complete OSE ownership entry

- Two-camera starter:
  - Target owner price: $400--$500
  - Purpose: Primary initial consumer package

- Four-camera system:
  - Target owner price: approximately $600--$800
  - Purpose: Comparable local recorder system

- Eight-camera system:
  - Target owner price: approximately $900--$1,400
  - Purpose: Qualified expansion, not starter bundle


The base recorder should still be engineered and qualified for eight cameras.
Customers start smaller and add cameras without replacing the software model or
learning a new setup process. If an honest eight-camera recorder cannot fit the
baseline price, offer a clearly priced capacity tier instead of weakening the
reliability claim.

See [tentative system requirements](requirements/requirements.md#product) for the
component price envelope and
[production hardware shortlist](archive/research/2026-09-27-production-hardware-shortlist.md)
for dated sourcing evidence. The
[camera market and owner-cost baseline](archive/research/2026-09-28-camera-market-baseline.md)
keeps the current Ring, Wyze, Reolink-kit, and ANNKE comparison out of durable
strategy claims.

<a id="product-strategy-decision-rules"></a>

### Decision rules

1. Optimize total owner value and trust, not the lowest promotional sticker
   price.
2. Ship fewer supported SKUs quickly rather than a broad, fragile compatibility
   list.
3. Do not lower security, local-operation, or recovery gates to hit a price.
4. Do not productize a development board when a finished sourceable product
   reaches the target faster.
5. Publish the self-build path even when OSE also sells a finished kit.
6. Separate verified capability from roadmap and aspiration.
7. Recheck current prices, subscription terms, and competitor features before
   publishing any external comparison.

<a id="product-strategy-first-sale-gate-sequence"></a>

### First-sale gate sequence

Market and sourcing research continue in parallel, but they do not reorder the
technical path to a responsible sale:

1. Prove reliable automatic operation on an ordinary home LAN, including
   camera route changes and outage recovery without owner networking work.
2. Move from development hardware to each exact production recorder, camera,
   power, storage, and networking SKU; rerun the complete acceptance contract.
3. Lock the BOM only after those exact revisions pass, then obtain controlled
   supply and landed-cost evidence. A marketplace listing is not a continuity
   agreement.
4. Build one complete system from new, factory-fresh parts using only the
   release artifacts and customer instructions.
5. Have the product owner personally perform the ordinary **Add Camera** and
   replacement journeys at home. Any unexpected shell, address, URL, YAML, or
   vendor-cloud step is a launch blocker, not a documentation footnote.
6. Run a small supported pilot, close recovery and support defects, then publish
   accurate Amazon/eBay offers. Marketplace automation may monitor evidence and
   prepare drafts, but it must not bypass SKU, seller, revision, quantity,
   price-ceiling, inventory, or human publication controls.

This sequence does not park competitive work. Dated market evidence should keep
testing price and package assumptions throughout development; it simply cannot
substitute for a repeatable factory-fresh owner experience.

<a id="product-strategy-distant-owned-hardware-path"></a>

### Distant owned-hardware path

OSE may eventually publish and manufacture its own camera electronics,
firmware, enclosure, and test process. Printable enclosure iterations and an
OEM/ODM bridge can be useful steps, but neither belongs on the first-sale
critical path. Any future entry gates and open-source deliverables need an
explicit owner decision; no prototype work is authorized.

<a id="product-strategy-documentation-boundary"></a>

### Documentation boundary

Use [current state](README.md) as the entry point. Requirements own the
product contract; design owns architecture, integration and the BOM; research
owns dated evidence. Archive retains research, reviews and measured evidence,
not superseded requirements, design copies or intermediate plans.
Do not create a second specification or shopping list inside a work brief.

<a id="hardware-and-bom"></a>

## Hardware and BOM

Use finished cameras and a completed Linux recorder appliance. A catalog
single-board computer in an OSE-sourced or manufactured enclosure is a permitted
controller assembly route, not a custom PCB project. Compare it with finished
mini PCs using the complete requirements and cost, not a board-only sticker price.

research/candidates/controller.md and research/candidates/camera.md are the
separate generated reports. Their matching .json files own exact configurations
and gates; controller-sources.json and camera-sources.json in the same directory
retain supporting facts and vendor observations.
The [requirements](requirements/requirements.md)
own camera/kit price targets and controller price-ranking policy. This section owns quantities and
physical interfaces; no exact controller or camera is approved by this design.

<a id="hardware-and-bom-historical-four-camera-parts-list"></a>

### Four-camera parts contract

The historical priced list is preserved in
research/historical-sourcing.json. For a four-camera
configuration, Wi-Fi quantity + PoE quantity = 4.

- One completed recorder: enclosure, PSU/cable, cooling, installed >=8GB RAM,
  reliable boot storage and >=1Gbps Ethernet.
- Four finished outdoor cameras with their included mounts and required power.
- One separate replaceable endurance-rated recording drive and its qualified
  data/power connection. Do not count boot storage as the recording drive.
- One set of missing ordinary mounts, weatherproof connections, Ethernet
  uplink and power accessories; do not double-count box contents.
- If any cameras use PoE: standards-compliant switch/injector capacity, cable
  glands and a measured simultaneous power budget.
- If any cameras use Wi-Fi: proven home-network coverage and continuous camera
  power. Wi-Fi carries video wirelessly, not power.
- Optional UPS, network upgrades, unusually long cables and installation are
  separately priced. Reused test storage needs explicit identity/health checks;
  never erase an unidentified disk or assume it covers retention.

For a board-based recorder, include the boot module/SSD, PSU and ordinary cable,
qualified cooling, housing material/manufacturing, fasteners, accessible ports,
drive adapters/cables and assembly/test labor in its complete cost. An assembled
OSE appliance must have a documented revision, repair/spares, recovery and
maintenance path. A lower board price cannot waive those obligations.

<a id="hardware-and-bom-storage-sizing"></a>

### Storage sizing

Decimal GB/day = camera count x measured main-stream Mbps x 10.8.
Required usable capacity = GB/day x retention days / 0.8, reserving 20%.

Four cameras at 4 Mbps need about 1.51 TB for seven continuous days before audio
and other overhead. A 2 TB drive is a planning baseline, not a tested retention
guarantee. Recheck actual bitrate, usable capacity and load; do not reduce image
quality to fit an estimate or infer continuous storage from event frequency.

<a id="hardware-and-bom-choices-still-requiring-approval"></a>

### Selection and approval

The current candidate file owns the exact sample proposal, unclosed camera/
controller evidence, buying links and production-cost verdicts. No research
comparison authorizes an order, custom PCB, production deployment or port.

Select one complete returnable configuration after mandatory technical and
fabrication-file gates pass, costs are disclosed and the owner approves acquisition.
Controller price ranks options; it is not an eligibility gate. Qualify both decode and inference;
a display specification or TOPS headline does not prove simultaneous performance.
The accepted system still needs the
[capacity test](requirements/requirements.md#performance-and-capacity).

<a id="hardware-and-bom-storage-mechanical-compatibility"></a>

### Storage mechanical compatibility

Before pricing a kit, match the drive's protocol, connector/keying, form factor,
thickness, power, cooling and supported module length to the exact recorder.
An M.2 slot does not imply both SATA and NVMe; a 2.5-inch bay cannot fit a
3.5-inch drive. Price any external enclosure, PSU and cables, and qualify
disconnect/failure behavior. Specific incompatible historical pairings are
evidence in the candidate file, not complete BOMs.

<a id="hardware-and-bom-purchase-gate"></a>

### Purchase gate

Record exact SKU/revision, firmware, seller, condition, included parts,
observation/quote date, stock claim, destination freight/duty, return/warranty
terms and total. Evaluate the [sourcing-cost rules](requirements/requirements.md#production-sourcing-cost),
[camera](requirements/requirements.md#cameras) and
[controller](requirements/requirements.md#controller-recorder) requirements
without borrowing another revision's capabilities. Any exception needs separate
explicit approval and must not be advertised as meeting the saleable contract.

<a id="four-camera-deployment"></a>

## Phase 2: four property cameras

Goal: four useful, continuously recorded property views on production-intent
hardware. Wi-Fi is the first choice where coverage and power permit; PoE is
an equal supported alternative. A mixed installation is valid.

<a id="four-camera-deployment-order-of-work"></a>

### Order of work

- 1. Approve BOM:
  - Done when: One recorder SKU and the necessary camera models pass the [buying gate](#hardware-and-bom-purchase-gate). No order is implicit.

- 2. Qualify one real camera per transport used:
  - Done when: Native main/substream video, local control, identity, reset/update and network recovery are verified on the exact firmware.

- 3. Finish Add Camera:
  - Done when: The customer can register the qualified camera without addresses, stream URLs, terminal commands or credential leakage.

- 4. Deploy four:
  - Done when: Four named views record and play back at the selected mounting
    positions. Installation-specific names and locations belong in the private
    deployment documentation.

- 5. Prove reliability:
  - Done when: The checks below pass; gaps and failures have measured causes, not guesses about Wi-Fi.


Steps 1-3 gate installation. Existing Pi cameras remain temporary fixtures;
their low frame rate is not a reason to weaken the new-camera contract.

<a id="four-camera-deployment-software-work-that-carries-forward"></a>

### Software work that carries forward

- Preserve native camera video; keep main recording and detection separate.
- Show recording, detection and viewing health separately. Missing data is
  Not checked, never proof of healthy recording or a fabricated RSSI.
- Preserve complete events and saved clips; report storage/backend failure
  visibly, never as an empty history. Verify the repaired storage recovery.
- Poll alerts separately from recording; request high-urgency visible pushes.
- Finish the exact camera's local setup driver after its interfaces are proved.

<a id="four-camera-deployment-acceptance-run"></a>

### Acceptance run

1. Record all four cameras for 24 hours while using live view, event playback,
   exports and notifications. Measure gaps and actual recorded-video FPS.
2. Perform at least 20 daytime and 20 nighttime walk-bys, distributed across
   all four locations. Check complete video, detection, alert and exact-event
   playback for each. Record every miss; do not infer detection from recording.
3. Repeat notification checks with the phone unlocked and locked, on Wi-Fi
   and cellular. Record event time, provider acceptance and observed phone arrival.
4. Test camera, router and recorder restart plus loss of Internet. Verify
   recovery without IP edits, duplicate cameras or damage to working views.
5. Test storage failure first in an isolated fixture. A real disconnect needs
   a maintenance window and preserved recordings; never deliberately corrupt
   the working drive for a demonstration.

Four-camera Phase 2 completion is not an eight-camera sales qualification.
Unmeasured physical checks remain pending even when automated tests pass.

<a id="camera-onboarding"></a>

## Camera onboarding design

Status: target flow with a partially implemented claim/state boundary. Secure
physical Wi-Fi transfer and an exact retail model's local setup driver remain
unqualified. See [Phase 2](#four-camera-deployment) and the
[acceptance contract](requirements/requirements.md#onboarding-and-identity).

The governing security floor is the
[security contract](requirements/requirements.md#security).
A plain-HTTP local portal must not collect the home Wi-Fi passphrase.

<a id="camera-onboarding-outcome"></a>

### Outcome

Both turnkey and DIY cameras enter the same owner flow:

```text
power camera -> scan label or enter camera code -> choose Wi-Fi -> connect -> name -> live
```

The owner never supplies an IP address, stream URL, DHCP reservation, router
rule, YAML file, SSH command, or terminal command. A DIY builder may use the
separate factory/build tools to create the ready-to-power device.

<a id="camera-onboarding-hardware-boundary"></a>

### Hardware boundary

OSE depends on these camera capabilities, not a Raspberry Pi model:

- permanent device identity and one-time physical claim proof;
- a bounded local setup transport;
- authenticated native-H.264 main and detection substreams;
- authenticated health, configuration, registration, and update operations;
- durable home-LAN credentials plus physical recovery/factory reset; and
- camera-initiated route registration after DHCP or access-point changes.

The setup transport is an adapter. DPP is preferred. A selected platform may
use Bluetooth or a camera setup access point only when an audited protocol on
top provides the authentication, encryption, replay resistance, and recovery
properties in the governing decision. The recorder's `VMS-*` network is a
recovery/test path until such a protocol is qualified; proximity or a private
SSID alone is not secure commissioning. Changing the adapter must not change
the owner flow or recorder state model.

<a id="camera-onboarding-what-the-customer-actually-does"></a>

### What the customer actually does

<a id="camera-onboarding-camera-sold-or-prepared-by-ose"></a>

#### Camera sold or prepared by OSE

- **Human does:** power it, scan the OSE QR or type the OSE camera code printed
  below it, complete the guided Wi-Fi step, and name it.
- **Automation does:** verify the one-time claim, permanent camera identity,
  exact hardware/firmware profile, home-network registration, and two advancing
  H.264 streams before showing Live.
- **Success:** no IP address, RTSP URL, vendor account, shell, or SD-card work.

<a id="camera-onboarding-supported-camera-bought-off-the-shelf"></a>

#### Supported camera bought off the shelf

An off-the-shelf vendor camera does **not** arrive with an OSE QR or OSE camera
code. It is supported only after an exact model/revision gets a model-specific
local setup driver.

- **Human does:** choose that exact supported model in Add Camera and perform
  only the documented physical reset or temporary cable step shown on screen.
- **Automation does:** discover the local camera, create fresh local
  credentials and an OSE claim, disable vendor P2P/cloud where the model allows
  it, verify identity and native streams, and then register it.
- **Success:** the owner still never enters an IP address or RTSP URL.

That second flow is the next implementation gap. The finished-camera adapter
enforces its evidence but no real vendor driver has passed it yet; the UI must
not advertise generic off-the-shelf compatibility before one does.

<a id="provisioning"></a>

## Provisioning

Factory/DIY preparation and customer setup are different operations.

- Prepared camera:
  - Human action: Power it; scan its QR or enter its printed code
  - Software action: Verify one-time claim and permanent device identity, securely commission, verify video, then register

- Supported retail camera:
  - Human action: Choose exact model; follow its physical setup step
  - Software action: Use its qualified local driver, set distinct credentials and verify identity/video

- Recorder:
  - Human action: Connect Ethernet, power and recording storage; open the local name
  - Software action: Create local owner access, verify storage and load authorized cameras

- Optional self-build:
  - Human action: Build/install the documented release and device payload
  - Software action: Reuse the same identity, verification and owner-facing boundaries


The [camera onboarding design](#camera-onboarding) defines the customer flow.
A retail vendor's QR is not automatically an application claim. No generic
vendor-free retail provisioning driver is qualified yet; see
[current state](README.md).

<a id="provisioning-preparation-boundary"></a>

### Preparation boundary

Images contain runtime packages and recovery services before first boot.
Device-specific identity and credentials remain in protected deployment state,
outside the product repository. Setup retries reuse identity; replacement or
factory reset is explicit. The browser must not collect home Wi-Fi credentials
as an insecure fallback.

The Linux recorder installer includes the portal, recording, alert and storage
services. The selected finished-camera model keeps vendor firmware unless an
explicitly qualified alternative is offered. Verify activation, configuration,
talk, reset and updates locally, not just its video stream.

Use the maintained [Linux installer guide](../platforms/linux/recorder/README.md)
for platform installation. Superseded factory/SD guides are not parallel product
instructions.

<a id="camera-route-handoff"></a>

## Camera route handoff design

Status: host-tested transaction and deployed private relay; physical route-change
and failure-injection acceptance remain. See [current state](README.md)
for remaining execution. [Onboarding requirements](requirements/requirements.md#onboarding-and-identity)
own the recovery contract.

<a id="camera-route-handoff-outcome"></a>

### Outcome

Changing DHCP address, mesh access point, or provisioning transport cannot
silently replace a working feed. One camera moves through this transaction:

```text
signed route announcement
  -> candidate route
  -> verify identity
  -> verify advancing main + detection media
  -> switch only that go2rtc source
  -> verify downstream frames
  -> commit

failure at any point -> restore/preserve previous route
```

Discovery is evidence, never authority to switch media.

<a id="camera-route-handoff-registry-relay-and-engine-boundary"></a>

### Registry, relay and engine boundary

- Registry schema 2 stores committed endpoints separately from candidates and
  reads existing schema-1 state safely.
- An authenticated heartbeat stages only its own candidate.
- The recorder verifies all declared unique media roles before promotion.
- Failed validation discards the candidate and leaves the previous route live,
  including across process restart.
- Legacy one-stream endpoints explicitly alias main and detection roles. This
  is compatibility behavior, not dual-stream product qualification.
- Endpoint-registry changes wake the bounded Frigate reconciliation service;
  the periodic timer remains recovery fallback.
- A bounded recorder-local relay exposes only strict, credential-free
  `/v1/cameras/<identity>/<selection>/<role>` sources to go2rtc. It resolves
  current registry state, obtains the camera secret from owner-private state,
  and injects authorization only in the upstream request header.
- The relay accepts only the configured private container subnet, suppresses
  request logging, rejects redirects and unexpected media types, limits live
  handlers, and never persists credentials in its selection state.
- Frigate can be configured with stable relay sources. Its container receives
  only a private host-gateway alias; the relay does not bind to the home LAN.
- A candidate is staged under a temporary go2rtc name and must produce two
  distinct decoded frames. The working name is then changed, and advancing
  go2rtc and Frigate frames must be proved before registry promotion.
- A downstream or registry failure reselects the committed route, restores the
  working go2rtc name, proves old frames, and discards the temporary source.
- Legacy schema-1 engine settings keep the relay disabled. Schema 2 carries an
  explicit `route_relay_enabled` flag so installation can be staged without a
  surprise source migration.

<a id="camera-route-handoff-credential-handling-constraint"></a>

### Credential-handling constraint

The go2rtc runtime update API accepts sources in URL query parameters, so it
cannot receive credential-bearing camera URLs under the OSE secret-handling
requirements. Use a loopback credential-injecting route adapter, a supported
secret reference, or another mechanism that keeps secrets out of URLs and logs.
Do not weaken the requirement to fit the API.

<a id="event-storage"></a>

## Event storage design

Status: software implemented and unit-tested; live owner acceptance remains pending.
See [current state](README.md). Retention and saved-clip
behavior are defined in [owner-experience requirements](requirements/requirements.md#storage).

<a id="event-storage-implementation-boundary"></a>

### Implementation boundary

Frigate remains the storage/event engine. OSE projects the product retention
choice into Frigate's motion and alert retention and uses Frigate exports for
saved clips. OSE does not add another database, retention worker, timeline, or
media-file layout. All mutation and media access remains behind the
owner-authenticated OSE application; the browser never receives the Frigate
port, camera binding, database, or filesystem path.

The independent legacy continuous-capture adapter follows the same saved OSE
retention value while it remains in the migration fixture. Its old media is not
presented as Frigate detection events.

<a id="media-delivery"></a>

## Media delivery design

Status: see [current state](README.md) for implementation and measurement status.
The paths below are
the durable design, not a fresh performance claim. Targets belong in
[owner-experience requirements](requirements/requirements.md#performance-and-capacity).

<a id="media-delivery-outcome"></a>

### Outcome

Recorded events begin and seek promptly through narrow, owner-authenticated OSE
routes, and live video has a measured, explainable cadence on the home network
and Tailscale. OSE reuses Frigate/go2rtc media facilities instead of building an
MP4 seek engine or another live-video stack. Recorder hardware is reconsidered
only after avoidable delivery costs are removed.

Recorded playback and live viewing remain separately measured paths:

1. **Recorded events:** Frigate HLS VOD through OSE-authenticated manifest and
   segment routes.
2. **Live viewing:** go2rtc WebRTC with an authenticated MSE-capable fallback,
   then protected snapshots only as the visibly degraded last resort.

<a id="media-delivery-corrected-recorded-event-design"></a>

### Corrected recorded-event design

The original OSE path downloaded Frigate's complete generated MP4 into one
`bytes` value, up to 64 MB, before interpreting the browser's `Range` header.
Later browser ranges could repeat that complete upstream fetch. That diagnosis
is valid, but a custom range implementation is not the preferred correction.

Frigate already provides VOD/HLS. The pinned 0.18 pilot was checked directly on
2026-09-23:

- `/vod/event/<review-id>/...` does not work because an OSE event is a Frigate
  review ID, not a Frigate object-event ID;
- the private review response identifies the bound camera and exact start/end;
- `/vod/<camera>/start/<start>/end/<end>/master.m3u8` returns a master playlist;
- its media playlist references an initialization MP4 and bounded M4S segments;
  and
- `/api/vod/...` is not the correct nginx-vod route on this release.

OSE must therefore resolve the namespaced review through its private engine
adapter, verify that its camera belongs to the recorder, and construct the
camera/time-range path internally. The browser may request only a validated
manifest or segment basename. It may never provide an upstream URL, Frigate
path, camera name, or time range.

Preferred flow:

```text
browser
  -> authenticated OSE event manifest
  -> rewritten authenticated OSE media playlist
  -> only the required authenticated OSE VOD segments
  -> loopback-only Frigate nginx-vod
```

Retain generated MP4 only as an explicit download or compatibility fallback.
That path must eventually stream with backpressure; it must not reconstruct
seeking by buffering and slicing the complete file.

Frigate documents the
[event VOD API](https://docs.frigate.video/integrations/api/vod-event-vod-event-event-id-get/),
and the
[Frigate media implementation](https://github.com/blakeblackshear/frigate/blob/dev/frigate/api/media.py)
shows the nginx-vod metadata path. OSE pins and tests one Frigate release rather
than assuming `dev` behavior.

<a id="media-delivery-authorization-boundary"></a>

### Authorization boundary

Every manifest, segment and fallback download stays behind OSE authorization.
The engine revalidates the bound review; a cached playlist or an existing player
session does not authorize unrelated media.

<a id="owner-interface"></a>

## Owner interface design

Status: partially implemented. Remaining execution belongs in
[current state](README.md). Acceptance lives in
[owner-experience requirements](requirements/requirements.md#owner-experience).

<a id="owner-interface-outcome"></a>

### Outcome

The owner uses one compact Feeds-first application surface. Selecting a camera
opens live video, events/history, camera controls, and diagnostics without
navigating to a second full web page. Global settings open as a compact panel
over the same Feeds shell.

“One surface” does not mean deleting stable URLs or putting every control on
the screen at once. It means one visual and navigation shell with progressively
disclosed panels.

<a id="owner-interface-target-shape"></a>

### Target shape

```text
Feeds
  -> select camera
     -> Live
     -> Events / history
     -> Recording and alerts
     -> Diagnostics

Settings
  -> Cameras / add / replace
  -> Storage
  -> Remote access and sharing
  -> Notifications
  -> System / update / owner access
```

- On a phone, use a compact bottom sheet or full-height in-app panel with one
  clear close/back action and no horizontal overflow.
- On a wider screen, the same component may become a dialog or side panel.
- Camera naming remains on the feed surface. Do not add a duplicate name field
  inside settings.
- Event cards and playback live inside the selected camera's panel. No popup
  browser window and no separate-looking Events site.
- Global settings are grouped and collapsed by default; advanced and support
  details remain deeper than ordinary owner controls.

<a id="owner-interface-compatibility-and-routing"></a>

### Compatibility and routing

- The root route renders the canonical Feeds shell and can encode the open
  camera, panel, or event in its URL so refresh and browser Back work.
- Existing `/events`, `/events?event=...`, `/events?camera=...`, `/settings`,
  `/recorder`, `/cameras`, and `/storage` entry points must continue to work.
  They should redirect to, or render, the Feeds shell with the correct panel
  open rather than preserve a second visual application.
- Change Web Push deep links and service-worker validation only after the new
  route is accepted. An old notification must still open the intended event.
- Keep authenticated media and API routes separate from the HTML shell. This is
  a UI consolidation, not an excuse to expose Frigate or collapse security
  boundaries.
- Prefer progressive enhancement: a failed script must leave a usable owner
  path instead of a blank application.

<a id="owner-interface-accessibility-and-mobile-behavior"></a>

### Accessibility and mobile behavior

- Use dialog semantics, managed focus, focus return, Escape/Back handling,
  scroll locking, useful headings, and labeled controls.
- Avoid nested scrolling regions where possible. At 360 CSS pixels wide, the
  active feed and its panel must fit without horizontal scrolling.
- Opening a panel must not restart an otherwise healthy live stream unless the
  selected view actually changes.
- Preserve the compact feed density: disconnected cameras stay out of the
  active grid and settings do not consume feed tiles.

<a id="diagnostics"></a>

## Central diagnostics

The recorder owns one bounded, timestamped diagnostic history:
`monitoring/health-history.json`. It is indented JSON for human reading and
machine analysis, not a collection of model-specific log files. The encrypted
recorder backup includes it. Open Settings -> System log in the authenticated
portal to read grouped recorder, recording-drive and camera details, select a
UTC timestamp, or download the JSON. The same page links to Raw service logs
for timestamped systemd output from the recorder or a camera. Choose an
allowlisted source and a 50, 100 or 200 line tail; download the selected tail
as versioned JSON. Log reads are owner-authenticated, read-only, bounded, and
redact common password, token, authorization and URL credentials. The portal
accepts no unit names or commands from the browser. The recorder's
unprivileged portal process invokes one exact privileged helper; camera logs
use the camera's existing authenticated local API. Unsupported or offline
sources are marked unavailable. These tails are fetched on demand; they are
not persisted in the support history or recorder backup.

Keep support entry points together on this page: the stable home-network portal
name, owner sign-in, system summary and exports, log tails, and remote-access
settings. The current Pi pilot shows its key-only shell login as
`ssh vms-recovery@osecameras.local`; never display or collect a private key.
Other recorder hardware must provide its own shell login from its platform
profile; it must not inherit the Pi account.

Both devices offer a bounded current-boot system journal. Recorder sources also
include the portal, capture, health monitor, alerts, maintenance, Frigate,
camera-route services, network services, kernel, and boot warnings. Camera
sources also include camera capture, network recovery, NetworkManager, kernel
and sensor messages, and boot warnings.

The support-report download includes the current camera summary and saved
history. Unknown fields or unexpected text are rejected before export. The
default collector keeps 120 one-minute samples and 256 state changes; sampling
interval and bounds are configurable.

Each sample correlates recorder boot, uptime, temperature, CPU load and power;
recording-volume health/capacity; and each camera's authenticated identity check,
sensor/capture state, snapshot result/latency, codec, Wi-Fi/recovery state,
runtime/power, engine frame/skip metrics and retained-video age. State changes
and device restarts are kept alongside recent snapshots. A retained-video index
does not prove playback; a snapshot does not prove continuous video.

<a id="diagnostics-live-rate-labels"></a>

### Live-rate labels

Keep the main camera tile short: browser view rate, Frigate detection-input rate
with skipped frames, and the age of the newest indexed video segment. The
expanded camera diagnostics explain the separate values:

- **View FPS** counts frames this browser displays. It is not the camera's
  source rate, detector rate, or saved-video rate.
- **Detection input FPS** is the camera-frame rate entering Frigate's detection
  pipeline. **AI checks/s** is how often Frigate runs object detection; it can
  be lower than the input rate.
- **Processed FPS** is frames Frigate processes. **Skipped frames/s** is the
  rate Frigate skips for processing; it does not by itself show that recording
  lost video. Frigate documents these as distinct camera metrics in its
  [metrics reference](https://docs.frigate.video/configuration/metrics/).
- **Video indexed ago** is the age of the newest main-stream segment in
  Frigate's recording index. It confirms an index entry, not that playback
  works.

Keep the JSON field names stable for machine analysis. Shorten only the human
labels; do not combine browser, detection, and saved-video rates into one
number.

The default minute sampling, CPU/temperature thresholds and retained counts are
parameters of the collector. Short failures between samples can be missed.
Unsupported fields are null or unknown, never assumed healthy. Historical power
flags mean a problem occurred since boot, not that it is happening now. Missing
camera power data cannot rule out a bad supply. An older outage cannot be
reconstructed when evidence was not retained.

For a bounded FPS check, `tools/fps_diagnostic.py` samples Frigate input,
processed, AI-check and skipped rates alongside host CPU/load/temperature. It
can probe recorder-ingress RTSP streams and count frames in saved clips with
`ffprobe`. The tool does not claim sensor FPS from a configured profile or
browser View FPS from recorder data; those require separate measurements.

<a id="diagnostics-device-boundary"></a>

### Device boundary

New hardware ports only its adapter: authenticate the device, obtain its stable
identity, read a bounded snapshot, and map vendor health/media/network/runtime
fields into the shared observation. The recorder collector and history format
remain common. Current native adapters use the existing `get_json('/health')`
and `get_bytes('/snapshot.jpg', maximum=...)` source interface; a vendor adapter
must normalize its own API to this interface. This does not imply support for
an arbitrary ONVIF camera today.

Raw service logs follow the same device-adapter boundary. The recorder maps
fixed product source names to systemd units; each supported camera adapter
exposes the same versioned log record through its authenticated API. A new
device may report its log source as unsupported; it must not broaden recorder
privileges or accept arbitrary remote commands.

No Raspberry Pi command belongs in a generic vendor adapter. The Linux runtime
helper and Pi power reader are optional collectors; unsupported interfaces
return unknown. Future adapters must pass the same identity-mismatch,
failed-device isolation, missing-measurement and secret-redaction checks.

Keep credentials, addresses, permanent hardware identifiers, images and raw
exception text out of the history. Store it owner-only and serialize writes
through a single recorder worker. Bound samples, events, input sizes and network
requests. Diagnostic collection must not enroll, reboot, repair or change
recording policy. Recovery belongs to separately bounded device logic.

The support snapshot should grow through this schema as additional evidence
collectors are validated. Complete causal diagnosis is a goal, not a claim that
every vendor exposes every needed signal.

<a id="notifications"></a>

## Notifications

```text
camera -> recorder detection -> event -> alert worker -> push service -> phone
```

The Linux alert service runs separately from video capture. It waits two seconds
after each completed poll; API/network time can extend that interval. Python
imports stay warm while owner settings and engine bindings reload each cycle.
Only status changes are logged. A process lock permits one send cycle at a time. A separate
state lock protects the private, bounded retry ledger across processes.

Visible event and test pushes use `Urgency: high`. Queued event messages keep
their one-hour TTL; tests keep one minute. This requests prompt delivery; it
does not bypass phone permissions, power policy or Do Not Disturb.

A successful send means the provider accepted it. The ledger's legacy
`delivered_at` field records that stage, not phone receipt or human visibility.
It is now timestamped after the send, not before it. A crash after acceptance
but before saving can still retry; a stable event tag coalesces notifications
where supported. This is not a claim of exactly-once delivery.

<a id="notifications-diagnose-a-delay"></a>

### Diagnose a delay

- No complete recording:
  - Check: Camera, stream, network and storage health first

- Recording exists, no detected event:
  - Check: Detection stream, model, zones and processing load

- Event exists, no accepted send:
  - Check: Alert enablement, enrollment, worker, private HTTPS setup and provider failure

- Provider accepted, phone arrives late:
  - Check: Phone/browser permissions, battery policy, Do Not Disturb, connectivity and push-provider delay


The service worker asks the browser to display the notification immediately.
Phone receipt/display acknowledgments are not yet measured automatically.
Use Settings -> Notifications -> Send test both unlocked and locked. Do not
call the locked-phone issue fixed until the real phone passes.

Operator checks, on the recorder:

```sh
systemctl status vms-recorder-alerts.service
journalctl -u vms-recorder-alerts.service --since '10 minutes ago'
```

Do not paste private subscription files, endpoints or keys into support reports.

Sources checked 2026-10-20:
[Web Push urgency](https://www.rfc-editor.org/rfc/rfc8030.html#section-5.3),
[Android Doze](https://developer.android.com/training/monitoring-device-state/doze-standby),
[pywebpush headers](https://github.com/web-push-libs/pywebpush).

<a id="integrations"></a>

## Alert Delivery And Integrations

This is a durable design and implementation reference. Production targets are
in [requirements](requirements/requirements.md); unfinished execution and physical
validation belong in [current state](README.md). References to Pi hardware
describe the existing adapter, not a production-hardware approval.

<a id="integrations-retail-experience"></a>

### Retail Experience

The owner configures alert delivery under **Alerts**. Email and text message
destinations are verified before use. A server connection asks for only:

- **Server address**
- **Access credential**

The credential is entered through a protected prompt and is never placed in a
configuration file or command argument. **Send Test** proves the complete
connection before it is enabled.

<a id="integrations-adapter-contract"></a>

### Adapter Contract

Email, text message, and server delivery are adapters over the same immutable
alert. An adapter receives a redacted alert envelope and a reference to its
credentials. It reports delivered, retryable failure, or permanent failure.
Provider-specific request formats and credentials remain inside the adapter.

Recording and detection do not wait for delivery. The recorder queues bounded
retries and reuses the original alert identifier for every attempt.

<a id="integrations-server-api"></a>

### Server API

The server address is an HTTPS base URL, using either a hostname or IP address.
The recorder sends an HTTP `POST` to `/api/v1/alerts`. It authenticates with the
protected access credential and sends `Content-Type: application/json` and an
`Idempotency-Key` equal to the alert identifier.

The version 1 body is intentionally small:

```json
{
  "schema_version": 1,
  "event_id": "EVT-0123",
  "occurred_at": "2026-08-15T12:00:00Z",
  "event_type": "motion",
  "camera": {
    "id": "CAM-0123",
    "name": "Driveway"
  }
}
```

The default payload contains no passwords, keys, contact destinations, image,
or recording. Media transfer requires a separate owner-enabled capability.

Any `2xx` response means delivered. Network errors, `429`, and `5xx` responses
are retryable. Other `4xx` responses are permanent configuration failures. A
test uses the same endpoint and envelope with `event_type` set to `test`.

<a id="integrations-deployment-boundary"></a>

### Deployment Boundary

The recorder's local web service is included in the signed recorder image and
starts automatically. The server integration is optional and outbound-only;
the recorder remains useful with no upstream server.

If the project supplies a companion receiver, it ships as a signed, versioned,
prebuilt package with one first-run owner setup. Deployment must not require a
customer to install a runtime, database, reverse proxy, or certificate by hand.

<a id="security"></a>

## Security

This is a durable design and implementation reference. Production targets are
in [requirements](requirements/requirements.md); unfinished execution and physical
validation belong in [current state](README.md). References to Pi hardware
describe the existing adapter, not a production-hardware approval.

- No shared factory password exists.
- Each kit has unique network credentials.
- Each kit records an explicit, validated Wi-Fi regulatory country; no country is inferred.
- Each device has a unique identity and private key.
- Cameras accept only their assigned recorder.
- QR codes contain a device identifier and one-time claim secret, never a private key.
- Provisioning mode ends after enrollment.
- Retrying an interrupted provisioning job does not create duplicate identities.
- Generated credentials and production manifests remain outside source control.
- Release inputs must be versioned, hashed, signed, and compatible with the target model.
- Diagnostic output excludes passwords, private keys, claim secrets, and stream credentials.
- The Zero W development-pilot profile is an explicit temporary exception to
  the product recovery policy: it enables per-device SSH, USB serial, GPIO
  UART, and capped persistent debug journals. SSH requires its per-device key;
  password authentication is disabled. The per-device console credential is
  pilot-only, no recovery credential is shared, and interfaces report only
  owner-private credential file locations.
- Product-tier images, including Zero 2 W, do not inherit the pilot recovery
  profile and continue to disable SSH and password login.
- Physical reset revokes the existing enrollment before reuse.
- Retail browser access and all off-device alert delivery use authenticated,
  encrypted transport. Plain HTTP is development-only and cannot pass product
  acceptance.
- Alert destinations, access credentials, and provider credentials are stored
  through the credential backend and are redacted from logs and exported
  diagnostics.
- The default server alert contains event metadata only. Images and recordings
  require a separate explicit owner choice.
- Outbound server delivery does not require router port forwarding or an
  Internet-accessible recorder.

The developer safety policy is external configuration. Public source contains the generic checks, not organization-specific identities, paths, remotes, or credentials.

<a id="remote-access"></a>

## Remote Access

This is a durable design and implementation reference. Production targets are
in [requirements](requirements/requirements.md); unfinished execution and physical
validation belong in [current state](README.md). References to Pi hardware
describe the existing adapter, not a production-hardware approval.

The recorder is local-first. Cameras connect only to the recorder, recording
continues without Internet service, and no setup path opens a home-router port.

<a id="remote-access-where-the-owner-opens-cameras"></a>

### Where the owner opens cameras

- At home: scan the recorder label or open `http://osecameras.local`.
- Away with Tailscale: open the private HTTPS address displayed after setup.
- Away with an owner gateway: open the customer's own HTTPS hostname.

An owner never needs to find a recorder or camera IP. On the pilot appliance,
`sudo vms remote status` prints both available browser addresses.

<a id="remote-access-one-recorder-command"></a>

### One recorder command

Run `sudo vms remote setup` and choose:

1. **Private access** — the recorder shows a Tailscale sign-in link and QR. The
   owner approves it in their personal tailnet once. The product clears Funnel,
   configures persistent private Serve, and prints the private HTTPS address.
   Each viewing phone or computer must run Tailscale and belong to that
   tailnet.
2. **Browser access** — the owner creates a fresh DigitalOcean droplet and
   points a hostname at it. The droplet's one installer prints a one-time code.
   The recorder asks only for that hostname and code, generates and pins keys,
   and prints the public HTTPS address. The owner needs no client app.

The two paths are alternatives, not a product-operated service. The customer
owns the Tailscale account or DigitalOcean account, all credentials, and all
recurring bills.

<a id="remote-access-gateway-security-boundary"></a>

### Gateway security boundary

The gateway installer enables a host firewall allowing only SSH, HTTP, and
HTTPS. HTTPS carries the one-time enrollment. The enrolled SSH key has no
shell, terminal, agent, local-forward, or wildcard-listen permission; it may
create only the gateway's loopback reverse port. Caddy is the sole public web
process and proxies that loopback port. The recorder pins the SSH host key
received through certificate-validated HTTPS and reconnects the outbound
tunnel after boot or network loss.

The owner's normal portal password remains required through either remote
path. Tailscale Serve is private to the owner's tailnet. Tailscale Funnel is
never used.

<a id="remote-access-live-media-boundary"></a>

### Live media boundary

Live WebRTC uses recorder-generated LAN and private-Tailscale candidates.
Signaling passes through the owner-authenticated OSE HTTPS application. The
separate media port carries ephemeral encrypted DTLS-SRTP sessions; it does not
expose Frigate's web UI, configuration, credentials, or unauthenticated API.
Browser clients fall back to protected same-origin snapshots if direct media
cannot connect. An owner-gateway deployment will require an explicit TURN or
other relayed-media design before WebRTC can be promised outside Tailscale;
the reverse HTTP tunnel alone does not relay UDP media.

<a id="remote-access-outcome"></a>

### Outcome

An owner never has to remember or type a recorder IP or long tailnet hostname.
Setup ends with **Open**, **QR**, **Copy link**, **Share**, and **Add OSE
Cameras to Home screen**. After that, the owner taps the OSE Cameras icon.

The local address remains `http://osecameras.local`. Remote access remains
optional and uses either the owner's Tailscale account or the owner's gateway;
there is no OSE SaaS, account service, redirector, or mandatory cloud backend.

<a id="remote-access-tailscale-constraint-and-product-answer"></a>

### Tailscale constraint and product answer

Set and retain the recorder's Tailscale machine name as `osecameras` when that
name is available. MagicDNS can resolve that short machine name, but Tailscale's
automatic HTTPS certificate is issued for the full
`osecameras.<tailnet>.ts.net` name, not bare `https://osecameras`. Tailscale
Serve also restricts its automatic names to the tailnet domain.

Therefore the default secure product solution is presentation, not a fragile
DNS trick:

- show the valid private HTTPS URL but do not require the owner to transcribe it;
- encode it in the recorder QR;
- use native browser sharing for Email, Messages, Nearby Share, and similar
  destinations instead of operating an OSE mail service;
- install the existing web application to the home screen with the name and
  icon **OSE Cameras**; and
- retain the link through DHCP changes and recorder restarts.

Do not trade a shorter URL for a certificate warning, plaintext HTTP, Funnel,
an inbound router port, or an OSE-operated redirect service. The official
[Tailscale HTTPS](https://tailscale.com/docs/how-to/set-up-https-certificates),
[MagicDNS](https://tailscale.com/docs/features/magicdns), and
[Serve](https://tailscale.com/docs/features/tailscale-serve) constraints are
part of this decision.

<a id="remote-access-customer-owned-gateway-option"></a>

### Customer-owned gateway option

The customer-owned DigitalOcean path may use a memorable hostname such as
`cameras.example.com` when the customer controls that domain and its DNS. The
gateway terminates valid HTTPS and keeps using the outbound, pinned recorder
tunnel. A customer without a domain needs a clearly supported provider-owned
hostname or must use the Tailscale/PWA path; OSE must not silently become the
DNS or SaaS operator.

<a id="remote-access-open-release-work"></a>

### Open release work

Live authorization, off-LAN testing, gateway packaging and setup-screen
integration remain pending; see [current state](README.md).
