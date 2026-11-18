> Historical reference. [Current documentation](../../README.md) takes precedence.

# Production hardware shortlist

In this report, **ONVIF** means a common local interface through which a
recorder can discover and configure a compatible IP camera. It reduces
vendor-specific integration work, but it does not by itself prove easy setup,
local-only operation, firmware ownership, or compatibility with every feature.

Status: historical sourcing direction, marketplace check 2026-10-03.
No purchase is authorized by this document. Its prices, shortlist and price
limits are not current requirements; use ../../research/README.md for maintained
candidates and source reviews.

Owner intake rule added 2026-10-03: the next proposed camera or recorder must
have an exact, independently findable Amazon or Alibaba listing. A vendor-only
page can corroborate specifications but cannot place a product on the active
owner shortlist. Marketplace discoverability still does not prove stock,
seller trust, revision continuity, local setup, or landed cost. Mapesen is
therefore excluded from active consideration.

## Price shape

- Recorder compute target: **$150--$200**, hard baseline ceiling **$250**.
- Standard camera target: **$75--$100**.
- Premium/night/doorbell camera target: **$100--$150**.
- One-camera starter: **$300--$400**.
- Two-camera starter: **$400--$500**.
- Four and eight-camera systems scale by camera and storage choice; eight
  cameras are not promised inside the starter price.

## Active recorder candidates

Only finished N150 mini-PCs remain active. OSE installs Linux; an included
Windows license is ignored or erased.

| Priority | Exact product | Current sourcing signal | Why active | Must close before purchase |
| --- | --- | --- | --- | --- |
| 1 | **ASUS NUC 14 Essential N150**, `RNUC14MNK150000U/2` family | Exact barebone observed at [$179.99 refurbished](https://www.ebay.com/itm/168598412987); new retail has been roughly $200--$290 | Official Ubuntu/RHEL validation, 24x7 product statement, 2.5 GbE, NVMe, defined SKU and vendor support | Obtain new-unit and 10/100-unit quotes including RAM; confirm exact NIC revision and current Linux driver; price complete recorder storage |
| 2 | **Beelink EQ14 N150**, 16 GB/500 GB, dual 2.5 GbE | Marketplace tracking observed about $179; [manufacturer configuration](https://us.bee-link.com/products/beelink-eq14-n150) is currently much higher and price-volatile | Finished enclosure, RAM/boot SSD included, two storage slots, dual Ethernet, three-year advertised warranty | Written exact-BOM/revision lock, Linux support, 10/100-unit quote, warranty/RMA terms, supply horizon, and 24x7 thermal evidence |

On 2026-10-03, a read-only [Amazon EQ14 listing](https://www.amazon.com/dp/B0D5XNYVHN)
exposed the N150/16 GB/500 GB product title, but not a dependable price, seller,
stock, or exact NIC/SSD revision. It is a directly searchable owner lead, **not**
a priced or qualified recorder. Do not carry forward the old ~$179 signal as a
current checkout price.

The ASUS is the lower-risk production platform; the Beelink is the price-floor
challenge. Neither receives an eight-camera claim from its processor name.
Purchase the cheaper exact unit only after the supplier confirms that the tested
SKU and internals are what later customers receive.

## Active camera candidates

The intended customer default is **Wi-Fi, not PoE**. PoE remains the reliability
control and an option for owners who already have suitable wiring; it must not
quietly become the default merely because it is easier to qualify. The fastest
first-sale path is a small curated set of finished, weather-rated, locally
streamable cameras. OSE must disable or avoid vendor P2P/cloud use and verify
provisioning/security behavior on the exact hardware revision.

### Hard Wi-Fi decision gates

A Wi-Fi camera is not a candidate merely because its product page says Wi-Fi,
RTSP, or ONVIF. One exact US SKU and hardware revision must pass all of these:

- current authorized US orderability plus a credible 10/100-unit supply path;
- 2.4/5 GHz operation, a documented antenna arrangement, DC power, and IP66/67;
- native H.264 main and detection streams with independently configurable
  frame rate and bitrate, without recorder transcoding;
- a permanent identity that survives DHCP changes and reset/re-enrollment;
- a local setup path OSE can drive or factory-complete without requiring the
  customer's vendor-cloud account;
- local RTSP/ONVIF behavior, vendor P2P/cloud disablement, firmware/version
  visibility, and signal or equivalent link-health telemetry;
- at the installed test location, at least 15 recorded frames/second, no gap
  over two seconds, no reconnect, and no visible event-playback stall during a
  20-minute motion test; and
- median RSSI of at least -67 dBm and fifth-percentile RSSI of at least -72 dBm
  when the camera exposes honest RSSI. A camera without RSSI must instead pass
  the same test with retry/loss and stream-continuity evidence.

These thresholds are OSE engineering acceptance estimates, not vendor claims.
A PoE camera is run beside the Wi-Fi sample as a diagnostic control: if both
stutter, investigate the recorder/playback path; if only Wi-Fi stutters,
investigate RF, retransmission, and camera buffering.

| Role | Exact product | Price signal | Relevant published fit | Open gate |
| --- | --- | ---: | --- | --- |
| Lead Wi-Fi desk candidate | **ANNKE WCD400P / I41DQ**, SKU `AU-I41DQ0104-OP` | [$69.99 sale / $79.99 crossed price, checked 2026-09-29](https://www.annke.com/products/wcd400p); page contains both Add to cart and back-in-stock controls, so current orderability is ambiguous | 4 MP 3840x1080 at 15 FPS, dual-band Wi-Fi 6, RJ45, DC or PoE, H.264+, ONVIF, IP66, power adapter included | Official I41DQ setup uses ANNKE Sight plus a registered account. Prove an OSE-driven local activation path, actual stock/revision, plain H.264 with separate detection stream, RTSP, cloud/P2P disablement, permanent identity, telemetry, and normal single-lens support burden |
| Baseline PoE control | **Reolink RLC-510A** | [$69.99 direct, checked 2026-09-27](https://store.reolink.com/us/cheap-security-cameras/); [Amazon ASIN `B08F568BH1`, $69.99, checked 2026-09-28](https://www.amazon.com/dp/B08F568BH1), sold by ReolinkDirect and shipped by Amazon | 5 MP H.264 main/substreams, 802.3af PoE, RTSP, local recording, IP67, microphone | ONVIF behavior, exact revision, firmware/update path, and no-cloud setup must be physically proven |
| Second-vendor PoE fallback | **Amcrest IP5M-B1276EW-AI** | [$74.99--$79.99 direct](https://amcrest.com/5mp-poe-camera-bullet-ai-ip5m-b1276ew-ai.html) | Finished 5 MP H.264 PoE camera, IP67, microphone, wide field of view | Confirm separate stream controls, exact ONVIF/RTSP behavior, firmware maintenance, component origin, and local-only setup |
| Doorbell sample | **Reolink Video Doorbell PoE, black**; target hardware `DB_566128M5MP_P` | [$119.99 direct, checked 2026-09-27](https://store.reolink.com/us/video-doorbells/) | [Manufacturer support](https://support.reolink.com/c/video-doorbell-poe/) states 5 MP H.264 main/substreams, RTSP/ONVIF, two-way audio, pre-motion recording, 802.3af PoE, alternate wired power, IP65, and two-year warranty | Lock the shipping revision; prove exact-event audio/video, chime, UID/P2P disablement, local-only operation, and revision stability |

Initial sale should support at most one baseline Wi-Fi camera, one PoE camera,
and one doorbell. A single-vendor first pass is acceptable for speed; the
Amcrest sample determines whether the OSE contract is genuinely vendor-neutral.

The Wi-Fi outdoor slot is **not qualified**. WCD400P is the strongest current
desk lead, but its own page gives conflicting stock signals and does not prove
plain H.264/RTSP, a separate detection stream, local no-account setup, or
revision continuity. Do not substitute a prototype, one-off marketplace unit,
or an H.265-only main-stream model to fill the gap.

### 2026-09-29 Wi-Fi candidate challenge

| Exact product | Evidence that helps | Evidence that blocks selection | Verdict |
| --- | --- | --- | --- |
| **ANNKE WCD400P / I41DQ**, SKU `AU-I41DQ0104-OP` | [The exact official page and specification, checked 2026-09-29](https://www.annke.com/products/wcd400p), publish $69.99 sale pricing, 4 MP 15 FPS, dual-band Wi-Fi 6, RJ45, DC/PoE, H.264+, ONVIF and IP66 | The listing simultaneously exposes Add to cart and a back-in-stock form. [ANNKE's exact I41DQ instructions](https://help.annke.com/hc/en-us/articles/61850006087449-Annke-Sight-How-to-Connect-the-camera-without-the-NVR-to-the-Annke-Sight-APP) require ANNKE Sight registration/login for standalone setup. It does not publish exact-model RTSP, separate main/substream controls, an OSE-drivable local activation interface, permanent identity, RSSI/link telemetry, or revision continuity. H.264+ must be proven usable as plain H.264 without recorder conversion. Its dual-lens panorama is not a neutral baseline form factor | **LEAD DESK CANDIDATE; HOLD SAMPLE**; reject as turnkey if the vendor-account step cannot be replaced locally |
| **Reolink RLC-510WA / W320 family** | Reolink publishes 5 MP, dual-band Wi-Fi, H.264, RTSP/ONVIF and IP67 for the family. A read-only check on 2026-10-03 found an exact [Amazon RLC-510WA listing, ASIN B08PYN7TS2](https://www.amazon.com/dp/B08PYN7TS2), but no dependable price, seller, or stock | The exact models remain absent from the current US Wi-Fi store. [Reolink's exact-model page](https://reolink.com/cz/product/rlc-510wa/) lists main/substream controls and RTSP/ONVIF but conflicting audio information across revisions/pages. Its [setup instructions](https://support.reolink.com/articles/360015776493-How-to-Initially-Set-up-Reolink-WiFi-Cameras-With-LAN-Port-and-without-Voice-Prompt/) require temporary Ethernet and the App or Client. OSE-owned local commissioning, exact shipping revision, audio, and US supply are unproved | **HOLD, do not buy**; exact Amazon discoverability clears only the owner's searchability requirement |
| **Reolink RLC-810WA** | [Current US store listing, checked 2026-09-29](https://store.reolink.com/us/wifi-security-cameras/): $117.99 sale / $129.99 regular, dual-band Wi-Fi and IP67 | Reolink's codec rule puts the 8 MP main stream on H.265, reintroducing a conversion/browser risk; setup still starts with temporary Ethernet plus Reolink software | **REJECT for current H.264 baseline** |
| **Reolink Lumus 4 MP** | [Current US store category, checked 2026-09-29](https://store.reolink.com/us/cheap-security-cameras/) shows $59.49 sale / $69.99 regular; [the exact product page](https://reolink.com/us/product/reolink-lumus/) publishes dual-band Wi-Fi, H.264 main/substreams at up to 15 FPS and IP65 | The exact product page omits RTSP and ONVIF from supported protocols and directs setup through a logged-in Reolink app/account. Reolink support says Lumus protocol support depends on hardware revision or a Reolink Hub/NVR, which defeats a simple standalone OSE BOM | **REJECT for standalone OSE use** despite attractive price |
| **TP-Link VIGI C340-W V2.20** | [Official US support, checked 2026-09-29](https://www.vigi.com/us/support/download/vigi-c340-w/) exposes exact revisions and an OpenAPI document; regional specs publish H.264 main/substreams, ONVIF/RTSP and IP66 | TP-Link says availability varies by region; no current authorized US product offer was established. It is 2.4-GHz-only and the exact local no-account setup path is unproved | **HOLD, do not buy** |
| **Reolink RLC-510A PoE** | Current US H.264/RTSP/ONVIF/IP67 price and supply evidence are stronger | Requires Ethernet to every camera | **Lab control and wired option, not Wi-Fi default** |

[Reolink's current setup instructions](https://support.reolink.com/articles/360015776493-How-to-Initially-Set-up-Reolink-WiFi-Cameras-With-LAN-Port-and-without-Voice-Prompt/),
checked 2026-09-29, explicitly cover RLC-510WA, W320, and RLC-810WA and
require DC power, a temporary router Ethernet connection, and the Reolink App
or Client. That is meaningful disconfirming evidence: published local streams
do not prove an OSE-grade customer setup experience.

**Current decision:** no Wi-Fi candidate clears all gates. Do not purchase a
batch. The first candidate to challenge is WCD400P, but even a one-unit sample
waits until the page's stock state, exact shipping revision, plain-H.264/RTSP
behavior, separate detection stream, and local commissioning path are resolved
from inspectable evidence. PoE is a one-unit diagnostic control, not a forced
product direction.

This is intentionally stricter than a feature-sheet comparison. WCD400P's RF,
price, and power options fit, but the currently documented customer setup does
not: a buyer is told to create and use an ANNKE account. OSE should not buy it
merely to discover that its most important ownership promise fails.

There is **no active battery/solar candidate**. Current duty-cycled cameras do
not yet satisfy the continuous local main/substream and recovery contract. Add
one only when an exact finished product proves local wake, live view, event
delivery, and no-cloud operation.

## Camera qualification state

`Published` below means desk evidence only; it is not an OSE acceptance pass.

| Exact product | Current retail source | Media fit | Local/security fit | Volume/revision fit | Next action |
| --- | --- | --- | --- | --- | --- |
| ANNKE WCD400P / I41DQ | Ambiguous official direct offer | Partial | Unproved | Unproved | Resolve published stock/setup/RTSP/revision evidence; then decide whether one sample is justified |
| Reolink RLC-510A | Yes -- direct and exact authorized Amazon offer | Published | Unproved | Unproved | Request locked quote; then one PoE sample |
| Amcrest IP5M-B1276EW-AI | Yes | Partial | Unproved | Unproved | Confirm missing stream/lifecycle facts before one fallback sample |
| Reolink Video Doorbell PoE black | Yes | Published | Unproved | Unproved | Request target-hardware quote; then one doorbell sample |
| Other Wi-Fi outdoor | No qualifying SKU | No candidate | No candidate | No candidate | Keep as fallback only; buy nothing |

If purchase is separately approved, the initial camera bench should contain
one RLC-510A and one black PoE doorbell. At prices checked 2026-09-27 that is
about **$189.98 before tax**. Add the previously observed $74.99 Amcrest sample
only after its missing product facts and current price are confirmed, for an
indicative maximum camera-sample total of about **$264.97 before tax**. These
are qualification units, not a product bundle.

## Investigated non-candidates

These remain recorded so they are not repeatedly rediscovered. They are not on
the purchase shortlist.

| Investigated hardware | Why it is not a production candidate | Re-entry condition |
| --- | --- | --- |
| ASUS NUC 14 Pro / Core Ultra 5 125H | Roughly $500+ before a complete sellable recorder; destroys the starter price | A separately priced high-capacity tier or a volume quote near the recorder ceiling |
| Raspberry Pi 5 + Hailo AI HAT | Board/accelerator assembly, cooling/enclosure and storage integration work; H.264 host-decode and shared PCIe topology remain risks; no price advantage after productization | A finished supported appliance SKU with locked BOM, warranty, storage, and measured cost/performance advantage |
| Existing Raspberry Pi 4 + Coral | Current CPU-only pilot failed scaling; Coral is not Frigate's new-install direction; weak 8/16-camera product envelope | None for new production; keep only as a compatibility fixture |
| RK3588 boards | Attractive codecs/NPU, but Frigate detector support and board BSP are community-maintained; enclosure/lifecycle integration remains ours | Finished vendor appliance, upstream support, reproducible Linux updates, and material cost advantage |
| Anonymous Alibaba N100/N150 mini-PCs | Mutable internals, no defensible lifecycle, Linux, warranty, certification, or revision guarantee | Named OEM, exact locked SKU/BOM, samples, certifications, RMA and quantity contract |
| Pi Zero/removable-SD camera | Weak Wi-Fi, SD-card/service burden, no separate native detection stream, no certified outdoor enclosure, assembly cost | None for the sold camera; retain only as documented DIY/legacy compatibility hardware |
| Reolink RLC-510WA | Technically attractive 5 MP H.264 dual-band Wi-Fi camera at $74.99, but Reolink marks it discontinued and the US store showed only eight remaining units | A genuine current successor or written production restart, revision lock, and volume lifecycle commitment |
| TP-Link VIGI C340-W | 2.4-GHz-only variants and US sourcing/revision/EOL ambiguity make the exact SKU unsuitable for a fast first BOM | Current US SKU, written lifecycle/quantity supply, dual-band or PoE transport, and exact revision test |
| Reolink CX410 | Good image and price, but current discontinuation/stock ambiguity makes it unsafe to standardize | Written lifecycle and volume availability for one exact revision |
| Reolink CX410W, Amazon ASIN `B0CNC15YJ1` | Published fit is unusually strong: fixed IP67 metal housing, 4 MP H.264 main/substreams, dual-band Wi-Fi, RTSP/ONVIF, Ethernet, and two-way audio. The exact Amazon listing is currently unavailable and the official US page exposes no purchase price. It also has no infrared mode, so dark-site performance depends on ColorX/visible light | Written current production and 10/100/1,000-unit availability, an orderable exact revision below $150, and a Driveway night test |
| Reolink E1 Outdoor CX, Amazon ASIN `B0CRKN329G` | Published 4 MP H.264 main/substreams, dual-band Wi-Fi, RTSP/ONVIF, two-way audio, and 24/7 recording fit, but the exact Amazon listing is currently unavailable. PTZ mechanics and IP65 add failure/weather risk that a fixed IP67 camera avoids | Current authorized US stock, written lifecycle/revision and quantity quote below $150, followed by local-only and outdoor qualification |
| Reolink T1 Outdoor / other legacy E1 Outdoor variants | Published H.264, dual-band Wi-Fi, two-way audio, and local protocols are promising, but exact US purchase availability, revision mapping, and quantity price are not established | Current exact US SKU, manufacturer lifecycle/revision commitment, quantity quote, and physical local-only test |
| Reolink Lumus Pro revisions | Retail price can fit, but model/revision capability mapping is ambiguous and current high-resolution stream codec behavior has not been locked | Exact current part/revision with H.264 main/substreams, local protocols, lifecycle, and quantity evidence |
| Current Reolink 8 MP Wi-Fi models such as Lumus Pro, E1 Outdoor Pro, and RLC-810WA | Manufacturer specifications put the high-resolution main stream on H.265 while the current interoperable baseline requires H.264 main; higher decode/browser work is not justified while a 4 MP H.264 candidate exists | Qualified H.265 browser/recording path or firmware with H.264 main output |
| TP-Link Tapo C120 | Its [current direct price](https://us.store.tapo.com/collections/color-night-vision-cameras/products/tapo-c120-indoor-outdoor-wired-security-camera) and [published 4 MP H.264, IP66, IR/color night, two-way audio, and RTSP/ONVIF](https://www.tp-link.com/us/home-networking/cloud-camera/tapo-c120/) make it a useful low-price benchmark. TP-Link's [third-party setup](https://www.tp-link.com/us/support/faq/2680/) requires the Tapo app, a TP-Link account, and creation of camera credentials; it is 2.4-GHz-only, and ONVIF does not expose two-way audio | A documented local commissioning interface that OSE can authenticate and use without a vendor account/cloud, plus identity, route handoff, stream, and RF acceptance |
| Current battery/solar retail cameras | Sleep/wake design conflicts with continuous recorder ingest; local RTSP/ONVIF and no-cloud wake are generally absent | Exact product passes the separate battery duty-cycle and local-only acceptance suite |

## External report reconciliation

The supplied hardware/camera intake, retained privately, contained three
concatenated research passes, not one internally consistent recommendation.
The reconciled findings below are historical analysis, not a current shopping
recommendation. Current candidates and source reviews start at
../../research/README.md. Moving the transcript does not verify any claim.

- The first pass recommends VIGI C340-W and a custom CM4 camera. VIGI remains
  technically interesting but fails current US/revision/supply evidence; CM4
  is outside the finished-product sourcing direction.
- The second pass promotes RLC-510WA. The third pass correctly supersedes that
  recommendation after the manufacturer marked the product discontinued.
- The third pass's final camera set largely matches this shortlist: RLC-510A,
  Amcrest IP5M-B1276EW-AI, and the black PoE doorbell stay sample-only.
- One contradictory claim said RLC-510A lacks audio. Current manufacturer
  product and support material explicitly list a built-in microphone, so that
  claim is rejected.
- A fresh screen found CX410W and E1 Outdoor CX technically credible, then
  rejected both from active sourcing because their exact Amazon US listings
  are currently unavailable. No report conclusion was accepted without a
  current, directly inspectable manufacturer or seller source.

## Competitive price correction

The checkout-price competition is harder than the original camera targets
suggested. On 2026-09-27, Ring advertised its 1080p Outdoor Cam at
[$39.99 sale / $79.99 regular](https://ring.com/outdoor-security-cameras) and
[Ring Multi recording at $99.99 per year](https://ring.com/plans). Four cameras can
therefore look like a $160 purchase during a sale, while recorded history makes
the verified three-year sale-price total about $460 and five-year total about
$660 before tax and accessories.

OSE should compete on customer-owned continuous recording, no mandatory annual
bill, repairability, and continued local operation--not pretend it can always
beat a subsidized camera sale on checkout price. Compare one-, three-, and
five-year owner cost in sales material, and always show whether recorder,
storage, continuous capture, and event history are included.

Wyze Cam v4 is a valid low-price competitor benchmark, not an active OSE camera:
the manufacturer advertises a
[$59.98 camera-plus-32-GB-card bundle](https://www.wyze.com/products/wyze-cam-v4-32gb-microsd-card)
and 24/7 local recording, but its standards-based recorder integration has not
cleared the OSE production contract. Eufy and packaged Reolink NVR systems are
useful subscription-free price references but do not replace the exact OSE
recorder and camera qualification. The durable competitive interpretation lives
in ../../design.md#product-strategy.

## Immediate sourcing action

The camera sourcing direction is now **finished retail proof plus finished
OEM/ODM volume screening**, not custom electronics. The public factory screen
and common evidence contract are in the
[2026-09-29 OEM/ODM sourcing report](2026-09-29-oem-odm-camera-sourcing.md).
It found leads, not a qualified product, and authorizes neither contact nor a
sample.

After contact is explicitly authorized, request written quotes rather than
conducting another platform survey:

1. ASUS: one, ten, and one hundred NUC 14 Essential N150 units, exact SKU,
   warranty, lifecycle, Linux/NIC revision, and barebone versus 16 GB pricing.
2. Beelink: the same for EQ14 N150 16 GB/500 GB, including an explicit internal
   component and motherboard revision lock.
3. ANNKE: before any contact or purchase is authorized, resolve public evidence
   for WCD400P stock, manual/local activation, RTSP/plain-H.264 and stream
   controls. If it survives, request one/ten/one-hundred-unit availability with
   an exact hardware/firmware revision lock and no substitutions.
4. Reolink business sales: RLC-510A and the black Video Doorbell PoE at one,
   ten, one hundred, and one thousand units, with exact hardware-revision,
   no-substitution, price, lifecycle, and support-lifetime commitments. Ask for
   the current fixed outdoor successor to CX410W/RLC-510WA with H.264
   main/substreams, dual-band Wi-Fi, two-way audio, RTSP/ONVIF, and IP67; do
   not assume such a successor exists.
5. Amcrest business sales: the same information for IP5M-B1276EW-AI.
6. Send the same finished-camera RFQ to the five named OEM/ODM leads. Require
   price breaks at 1/10/100/500/1,000, a locked exact BOM/firmware revision,
   local no-account commissioning, permanent P2P/cloud disablement, unique
   credentials, authenticated updates, certificate identifiers, warranty/RMA,
   and no substitutions. Treat **$30--$40 fully landed** as the 500/1,000-unit
   gate, not as a known supplier price.

Then buy no more than one recorder and one of each selected camera for the
existing acceptance tests. Do not reopen prototype platforms unless every
active production candidate fails a stated price or technical gate.

## Supply-channel sequence

Use retail and bulk channels for different jobs:

1. After purchase approval, use the exact manufacturer or authorized Amazon
   listing to obtain one traceable qualification sample. Automation may assist
   checkout, but must not substitute a different seller, revision, or bundle.
2. In parallel, after contact authorization, request direct manufacturer,
   distributor, and named OEM/ODM quotes at 1, 10, 100, 500, and 1,000 units
   with revision locks, no-substitution terms, lifecycle, firmware security,
   certification, warranty, and RMA evidence.
3. Use Alibaba and similar marketplaces only to discover and corroborate a
   named factory. An anonymous listing is not volume supply evidence and must
   never inherit acceptance results from a retail camera that merely looks
   similar.
4. Order at most one or two ODM samples only after the written screen passes
   and purchase is separately approved. Qualify each factory, BOM, firmware
   build, certificate set, and sample as a distinct product.

The distant option to own the camera electronics and enclosure is described in
../../design.md#product-strategy-distant-owned-hardware-path. It is parked
until the finished-product launch path and its unmet needs are measured.

## Read-only source watch

Historical watch-source identities and extraction terms are now retained in the
single [candidate document](../../research/production-candidates.md#research-provenance).
Old active labels are not current qualification; the checker reads only the
ranked comparison links in that document. Run
`tools/check-sourcing` from the repository root to collect page reachability,
exact-identity markers, explicit availability markers, and structured offers.
The checker never signs in, adds to a cart, contacts a seller, or places an
order. A reachable listing remains only a lead; marketplace blocks and missing
structured data are reported as unknown instead of being guessed.

The 2026-09-28 automated pass found positive page-level availability signals
for Beelink EQ14, Reolink RLC-510A, and the Reolink PoE doorbell. Amcrest and
eBay blocked the read-only fetch, Alibaba did not expose enough exact identity
data, and Amazon did not expose the watched ASIN identity to an unauthenticated
request. Those results are **unknown**, not product rejection or availability
proof. The dedicated visible Amazon adapter was subsequently verified under
host resource limits. It found the exact RLC-510A offer at $69.99 on 2026-09-28,
in stock, sold by ReolinkDirect and shipped by Amazon. Search and product-page
inspection are working; no cart or purchase action was taken.

The [Alibaba EQ14 listing](https://www.alibaba.com/product-detail/Beelink-Mini-S12-Pro-Mini-PC_1600755523555.html),
checked 2026-09-27, is useful because it identifies Shenzhen AZW Technology
(Group) Co., Ltd. as a named Beelink factory lead, but its published 16 GB/500
GB price is **$346--$395 with MOQ 2**. That fails the recorder price ceiling and
does not include a BOM lock. Keep it as an RFQ path only unless a written
exact-configuration quote changes the economics.
