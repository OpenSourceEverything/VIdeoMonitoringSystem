> Historical reference. [Current documentation](../../README.md) takes precedence.

# Historical purchase screen — superseded snapshot

This preserves the initial October 3 screen as dated research, not canonical
requirements or current approval. Use [requirements](../../requirements/requirements.md)
for the contract and the [marketplace sourcing check](2026-10-03-marketplace-sourcing-check.md)
for updated prices and automation evidence. No purchase is authorized.

# Production-intent camera and recorder screen — 2026-10-03

Status: **no item approved for purchase**. This is a read-only decision screen,
not a supplier-contact, cart, or buying instruction. It narrows the existing
[system requirements](../../requirements/requirements.md#product) and
[camera qualification](../tasks/camera-product-candidate-research.md) to the next exact
items a human can inspect on Amazon or Alibaba.

The [October 3 marketplace sourcing check](2026-10-03-marketplace-sourcing-check.md)
supersedes this page's automation status and adds the closer **$659.99
four-camera Wi-Fi Reolink kit** comparison. It does not approve a purchase.

## Working checklist and repeatable workflow

Use this page as the hardware-shopping entry point. The requirements below
are targets; no camera or recorder has passed them yet.

| Device | Shopping requirements |
| --- | --- |
| Camera | Finished outdoor, continuously powered Wi-Fi camera; at least 1080p/15 FPS; night vision and two-way audio; simultaneous plain H.264 main and lower-resolution substreams; authenticated local RTSP/ONVIF; setup/reset without a required vendor account or cloud; unique credentials, updates, warranty and repeatable supply. Prefer 2.4/5-GHz Wi-Fi; a 2.4-only camera needs a successful range/recovery test. |
| Recorder | Finished Linux device; wired Gigabit Ethernet; supported hardware video decode and accelerated detection; 16 GB RAM for initial qualification; eight recordings plus eight 5-FPS detection streams, under 1% skipped detection frames and 30% processing headroom; endurance-rated storage sized for seven days; tested restore/recovery and `osecameras.local` access. CPU model alone does not establish capacity. |

Architecture: camera H.264 main stream goes directly to recording; the
substream goes to recorder detection. Avoid routine video transcoding.

Price targets are **customer prices**: standard camera $75–$100; recorder
compute with RAM/boot storage $150–$200, $250 ceiling. Recording storage is
additional. A viable landed BOM still has to be established.

1. **Find exact listings:** AI searches Amazon, Alibaba and eBay. A blocked
   search can use a public search-index lead or a URL found in the human's
   browser. Inspect the live exact product page before keeping a lead.
2. **Screen:** AI returns at most three cameras and three recorders with a
   direct marketplace link, exact configuration, seller, quantity price/MOQ,
   shipping visibility, and pass/fail/unknown against this checklist. Unknown
   required features keep a candidate on hold; resolve them before selecting it.
3. **Choose:** human reviews one exact camera and one recorder with the total
   delivered qualification cost. Approval applies to that configuration and
   spending ceiling.
4. **Qualify:** test customer setup, 20 day plus 20 night walk-bys, complete
   recordings/playback, restart recovery and a 24-hour soak; then run the
   recorder's eight-camera load test. AI records results and diagnoses failures.
5. **Pilot:** review a small commercial batch only after technical acceptance,
   exact supply evidence and complete contribution costs are documented.

Exact-page research works for some Amazon and Alibaba offers. Alibaba search
is intermittent and eBay public search is currently blocked. This workflow
can proceed using exact links while those access limits remain.

## 1. Camera: what a sellable first SKU must do

**Hard gates (all must pass on the exact hardware/firmware revision):**

1. Finished, outdoor-rated, mains-powered Wi-Fi camera with a documented US
   seller and repeatable supply; at least 1080p/15 FPS, night coverage, two-way audio,
   and mounting/power accessories. Wi-Fi is the default, not PoE; 2.4/5 GHz is
   the current RF target. A 2.4-only exception requires a successful real-home
   range/recovery test, not an assumption.
2. Simultaneous *plain* H.264 main and lower-resolution substreams over local
   authenticated RTSP/ONVIF. H.264+ or an H.265-only main stream is not a pass.
   Record the main stream without routine video transcoding; detect from the
   substream. Confirm valid saved segments and browser playback, not just a
   live picture. [Frigate documents codec and segment failure modes](https://docs.frigate.video/troubleshooting/recordings/).
3. Owner can activate and reset it without a required vendor account/cloud,
   configure distinct per-camera credentials, and keep recording through an
   Internet outage. No port forwarding or vendor P2P required. A camera that
   needs a vendor app can be a separately disclosed BYOC compatibility path,
   but not an OSE-sold no-account kit camera.
4. OSE can bind a stable device identity to a changing DHCP route, reject an
   unpaired duplicate, and verify main/substream frames before promoting a
   route. One camera's setup/recovery must not restart other feeds.
5. Exact revision, firmware-update responsibility, certification, warranty,
   return path, ten-unit landed cost and 100/1,000-unit quote are documented.
   A marketplace card or prototype unit does not prove this.

**Acceptance test:** human resets and commissions one camera as a customer
would, then walks by twenty times by day and twenty by night. AI compares the
written walk-by times against camera delivery, Frigate detect input, actual
saved segments, detections, alerts, and complete playable exports. Repeat
through camera/router/recorder/Internet restart and a 24-hour soak. Treat any
missing person or partial clip as a failed layer to diagnose, not a pass.

### Camera shopping results, cheapest credible lead first

| Exact lead | Observed price / source | Requirement result |
| --- | --- | --- |
| [ANNKE WCD400P `AU-I41DQ0104-OP`](https://www.annke.com/products/wcd400p) | Vendor page displayed **$69.99 but Sold out**, 2026-10-03. No verified Amazon/Alibaba exact offer. | **Reject now.** Dual-band and ONVIF are vendor claims, but spec says H.264+ and G.711. Plain H.264, safe audio handling, separate substream, and no-account setup are unproved; vendor [setup uses ANNKE Sight](https://help.annke.com/hc/en-us/articles/61850006087449-Annke-Sight-How-to-Connect-the-camera-without-the-NVR-to-the-Annke-Sight-APP). |
| [TP-Link Tapo C120, Amazon ASIN B0CH45HPZT](https://www.amazon.com/dp/B0CH45HPZT) | Exact-title Amazon lead; price, seller and stock **not returned** by automation, 2026-10-03. [TP-Link specs](https://www.tp-link.com/us/home-networking/cloud-camera/tapo-c120/) claim 4 MP H.264, IP66, 2.4-GHz Wi-Fi. | **BYOC research only.** TP-Link's [RTSP/ONVIF instructions](https://www.tp-link.com/us/support/faq/2680/) require the Tapo app to create stream credentials; 2.4-GHz-only does not clear the default RF target. No OSE-sold no-account claim. |
| [Reolink RLC-510WA, Amazon ASIN B08PYN7TS2](https://www.amazon.com/dp/B08PYN7TS2) | Exact-title Amazon lead; price, seller and stock **not returned**, 2026-10-03. [Reolink specs](https://cdn.reolink.com/files/docs/specs/reolink_product_family.pdf) list dual-band Wi-Fi/H.264. | **Reject production supply.** [Reolink says discontinued](https://reolink.com/ca/product/rlc-510wa/); it is not a repeatable SKU. Also only microphone, not two-way audio, in its family spec. |
| [Reolink RLC-510A](https://cdn.reolink.com/files/docs/specs/RLC-510A-IP-Camera-Specifications.pdf) | Price and stock not freshly verified. | PoE **control only**, not the wireless customer default. Useful to isolate Wi-Fi versus recorder failures after an exact source is approved. |

**Result:** there is no verified current price for a camera meeting all hard
gates, so “cheapest qualifying camera” is **unknown**, not $69.99. Alibaba's
public search was blocked, and one exact page returned `Product Not Available`
despite stale `$0.99` metadata; neither is usable cost evidence. See the
[automation proof](../../../../amazon-automation/adapters/marketplace-readonly/README.md)
for the tool boundary; this relative link may not resolve outside the local
multi-repo workspace.

## 2. Recorder: what a sellable first device must do

1. One wired Gigabit-or-faster home-LAN port; local operation without an OSE
   account, cloud, or inbound port. Stable `osecameras.local` owner entry and
   a tested restore path after drive/OS failure.
2. Linux image and video-decode/inference path supported by the pinned Frigate
   release; native H.264 camera video is copied for recording, with no routine
   video transcode. Intel iGPU/OpenVINO is a candidate, **not** a capacity proof.
   [Frigate detector support](https://docs.frigate.video/configuration/object_detectors/).
3. Eight concurrent main recordings plus eight 5-FPS detection substreams,
   less than 1% skipped detection frames, and at least 30% measured processing
   headroom while one owner views and exports a clip. Test sustained thermals,
   RAM, storage writes, recovery and nearly-full disk. The exact CPU label is
   insufficient.
4. At least 16 GB configurable RAM for the first qualification, a reliable
   boot drive and an endurance-rated recording drive (or a single-drive design
   with an explicit failure/restore plan). Recording volume must be checked as
   mounted/writable; no silent fallback to boot media. Product storage target
   is seven-day retention at qualified bitrates, calculated from measured
   streams, not the nominal SSD capacity.
5. Exact model/revision, Linux NIC driver, firmware/update method, power
   supply, warranty, spare parts and full landed ten-unit price are known.
   Compute alone targets $150–$200, $250 ceiling; if impossible, revise the
   offer honestly rather than omit storage/support cost.

### Recorder shopping results

| Exact lead | Observed price / source | Requirement result |
| --- | --- | --- |
| [ASUS NUC 14 Essential N150 barebone, Amazon ASIN B0F7X6S2Q7](https://www.amazon.com/dp/B0F7X6S2Q7) | Exact-title Amazon lead; price, seller, stock **not returned**, 2026-10-03. A separate [reseller page](https://www.hecdistribution.com/shop/consumer-electronics/rnuc14mnk150000u-rnuc14mnk150000u/) displayed $231.47 barebone earlier on 2026-10-03; not an Amazon quote. | **Technical lead, commercial hold.** [ASUS specs](https://dlcdnwebimgs.asus.com/files/media/53b7cdd6-e9d1-4779-a6cd-ec5d0d775303/asus-nuc-14-essential-datasheet.pdf) indicate N150, RAM/NVMe options and 2.5 GbE; exact variant/slot count must be checked. [ASUS Linux note](https://www.asus.com/support/faq/1056488/) flags a Realtek LAN driver issue on Ubuntu 24.04. Barebone price already strains the compute ceiling before RAM/storage. |
| [Beelink EQ14 N150/16GB/500GB, Amazon ASIN B0D5XNYVHN](https://www.amazon.com/dp/B0D5XNYVHN) | Exact-title Amazon lead; price, seller, stock **not returned**. [Beelink direct](https://us.bee-link.com/products/beelink-eq14-n150) shows **$385, pre-order**, 2026-10-03. | **Comparison, not low-cost approval.** Dual M.2 and dual 2.5 GbE are attractive; shipped SSD is SATA per vendor. It exceeds the compute target and needs exact Linux/thermal/drive testing. |

**Result:** no recorder has a verified Amazon/Alibaba current checkout price
*and* eight-camera validation. The ASUS is the first hardware/price comparison
to investigate; Beelink is the two-drive comparison. Neither is approved.

## 3. Lock-step next actions

1. **AI:** keep the marketplace adapter read-only, report each site's blocks
   and missing prices honestly, and prepare exact SKU/variant links. **Human:**
   if convenient, open the exact Amazon leads and report only displayed price,
   seller, stock and variant; do not buy or share login credentials.
2. **AI:** deploy and live-validate the new on-demand retained-segment coverage
   check on the current recorder once authorized access is available. It is
   tested locally but not deployed; its index result does not prove playable
   video. Current Frigate `camera_fps` is detect input, **not** a recording metric. **Human:**
   no reflash or Pi ribbon work; use the existing Diagnostics stream check only
   to separate camera delivery from Frigate detect-ingest at that instant.
3. **AI:** present one exact camera and one exact recorder with a full landed
   price and remaining failures. **Human:** approve or reject each exact sample
   and spend ceiling. No ten-unit order before saved-video and walk-by proof.
