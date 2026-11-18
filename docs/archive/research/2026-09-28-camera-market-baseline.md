> Historical reference. [Current documentation](../../README.md) takes precedence.

# Camera market and owner-cost baseline

Status: dated decision evidence, checked 2026-09-28. Prices are US signals
before tax and installation. Sale prices can disappear. This is not public
marketing copy and does not qualify any camera for OSE.

## Decision

OSE should not try to win every checkout-price comparison. It must reach a
credible **$600--$800 four-camera local system** while beating conventional
NVR kits on setup, alerts, playback, recovery, openness, and support. If that
experience is not materially better, the price premium is not defensible.

The useful competitive anchors are:

- **Ring** for the everyday app, alert, live-view, and doorbell experience;
- **Wyze Cam v4** for low-cost wired Wi-Fi plus camera-local recording;
- **Reolink PoE NVR kits** for the closest subscription-free IP-camera system;
  and
- **ANNKE 3K Lite** for the brutal low-price complete-system floor, with the
  important caveat that its included cameras are 2 MP analog/BNC endpoints.

## Four-camera economics

Three-year totals below are arithmetic estimates from the cited price and
annual plan. They exclude tax, installation, optional mounts/power accessories,
disk or card failure, electricity, and temporary promotions after the check
date.

| System | Dated hardware signal | Recording needed for a useful comparison | Three-year owner spend | What the number really means |
| --- | ---: | ---: | ---: | --- |
| **Ring Outdoor Cam, four cameras** | [$39.99 sale / $79.99 regular each](https://ring.com/outdoor-security-cameras): **$159.96 / $319.96** | [Ring Multi, $99.99/year](https://ring.com/plans): **$299.97** | **$459.93 sale / $619.93 regular** | Polished remote/event experience and up to 180-day event playback. Without a plan, live view and alerts remain, but [reviewing recorded camera/doorbell video requires a subscription](https://ring.com/support/articles/03vq7/Ring-Video-Doorbell-and-Security-Camera-Frequently-Asked-Questions). It is cloud/account dependent, not an owner-controlled local recorder. |
| **Wyze Cam v4 + 32 GB, four cameras** | [$59.98 each](https://www.wyze.com/products/wyze-cam-v4-32gb-microsd-card): **$239.92** | Local 24/7 microSD: **$0**; optional [Cam Unlimited $99.99/year](https://www.wyze.com/pages/service-plans): **$299.97** | **$239.92 local / $539.89 with cloud plan** | [24/7 camera-local recording needs no plan](https://support.wyze.com/hc/en-us/articles/32776621952539-Is-24-7-Local-Recording-going-away), but no-plan local footage is manually reviewed and cloud event video/AI/web access are plan features. Setup and playback still center on the vendor app/account; this is not an open RTSP/ONVIF recorder baseline. |
| **Reolink RLK8-520D4-5MP, four cameras + 8-channel NVR + 2 TB** | Amazon ASIN [`B07H3QVZQ2`](https://www.amazon.com/dp/B07H3QVZQ2), **$542.99 list-price signal supplied 2026-09-28** | No required recording subscription | **$542.99 signal** | Closest checkout comparison to OSE: one local PoE system with disk. The [kit-family specifications](https://cdn.reolink.com/files/docs/specs/RLK8-520D4-NVR-Kit-IP-Camera-Specifications.pdf) publish 5 MP H.264, main and substreams, 30 m IR, PoE, microphone, person/vehicle detection, and 24/7 recording. The live Amazon selling price, exact shipped suffix/revision, seller, and support lifetime still need verification. |
| **ANNKE 3K Lite, eight cameras + 8-channel DVR + 1 TB** | [ANNKE direct SKU `AU-DW81KD1-V7-58BH-V2-P`, $254.99](https://www.annke.com/products/annke-3k-lite-security-camera-system); Amazon ASIN [`B00L3W2QJ2`](https://www.amazon.com/dp/B00L3W2QJ2), $334.99 list-price signal supplied 2026-09-28 | No required recording subscription | **$254.99 direct signal** | Extremely strong camera-count value, but the included cameras are 1920x1080 analog/BNC, not eight independent IP cameras. “3K Lite” describes DVR capability, not the included cameras. The DVR supports human/vehicle detection, H.265+, app/email alerts, and operation without Internet. It is a complete-system price competitor, not an OSE endpoint candidate. |
| **OSE four-camera system** | **$600--$800 target**, not a current offer | Customer-owned recorder and storage; no required subscription | **$600--$800 target** | Must include the common event model, usable local history, alerts, exact-event playback, repair path, and continued LAN operation. Open source and no rent do not excuse worse setup or playback. |

### Doorbell checkpoint

Ring's current entry wired and battery doorbells are each advertised at
[$39.99 sale](https://ring.com/video-doorbell-cameras), with regular prices of
$79.99 wired and $99.99 battery. A single useful recorded-history experience
adds Ring Solo at $49.99/year, producing estimated three-year totals of
**$189.96 sale**, **$229.96 wired regular**, or **$249.96 battery regular**.
Optional chimes, mounts, solar, installation, and 24/7-recording add-ons are
not included.

The OSE doorbell endpoint target of $100--$150 is therefore viable only as part
of a local system value proposition. The current $119.99 Reolink PoE doorbell
sample signal leaves no sustainable resale margin without direct supplier
terms.

## Experience and architecture matrix

`Published` means manufacturer documentation, not an OSE bench result.
First-frame time, notification latency, false positives, roaming recovery, and
night-image quality remain physical tests for every system.

| Dimension | Ring | Wyze Cam v4 | Reolink PoE kit | ANNKE 3K Lite | OSE requirement |
| --- | --- | --- | --- | --- | --- |
| Primary owner experience | Mature phone app, remote live view, alerts, event timeline, multi-camera live features | Phone app, live view, alerts, per-camera SD playback; cloud timeline/AI with plan | Vendor app/client plus local NVR monitor and storage | DVR monitor plus vendor app/email remote access | One local-first UI with phone alerts, exact-event deep links, grid/live view, and recovery |
| Recording ownership | Cloud by default; Ring Edge local storage needs Alarm Pro, microSD, linked accounts, and a compatible subscription | Local microSD is available; each camera owns a separate card | Local 2 TB NVR | Local 1 TB DVR | Recorder-owned storage and export using open, documented software |
| Internet loss | Ordinary cameras require home Wi-Fi/Internet for the intended service; 24/7 cloud recording requires stable Internet | Configured microSD recording can continue during Wi-Fi loss, but live control/app access does not | Local NVR recording is the architectural advantage; exact remote/P2P behavior needs test | Manufacturer claims local operation without Internet; remote features naturally do not | Recording, detection, local live view, and playback continue; remote access reports degraded state cleanly |
| Media path | Proprietary cloud endpoint; not an RTSP/ONVIF source | Camera-local/vendor ecosystem; standards-based OSE ingest not established | Published H.264 IP main/substreams and PoE | Eight included analog BNC feeds terminate at the DVR; only limited separate IP-camera capacity | Authenticated H.264 main plus simultaneous detection stream into the OSE recorder |
| Subscription boundary | Recorded-event review and advanced detection are plan features; Multi is $99.99/year | Continuous SD recording is free; smart/cloud event experience is paid | No recording subscription required | No recording subscription required | No required OSE or vendor subscription for the core product |
| Repair/vendor exit | Closed devices and cloud service; exported clips survive, service-dependent experience does not | SD media is owner-held, but app/firmware ecosystem remains vendor-controlled | Replaceable disk and standard cabling; camera/NVR firmware remains vendor-controlled | Commodity coax/power cabling and disk; DVR/app firmware remains vendor-controlled | Published source/build path, replaceable storage/endpoints, documented export and recovery |

## What this changes

1. **Do not promise lowest checkout price.** ANNKE can sell eight lower-spec
   analog cameras, a DVR, and a disk for less than OSE's recorder target alone.
   Matching that would require a different product and would destroy the OSE
   endpoint/security contract.
2. **Treat $542.99 as the immediate four-camera pressure point.** A $600--$800
   OSE system is only $57--$257 above the Reolink list-price signal. That
   premium must buy visibly faster setup, better event finding, local-first
   resilience, open implementation, and credible support.
3. **Do not sell “no subscription” as the whole story.** Wyze and both NVR kits
   already offer local recording without a required recording fee. OSE's
   differentiation is one coherent recorder-owned event experience without a
   vendor account or proprietary camera silo.
4. **Protect margin before adding SKUs.** At a $75--$100 camera price and a 40%
   gross-margin design target, landed cost must be about **$45--$60 or less**.
   A $69.99 retail sample is technically useful but is not a wholesale model.
   At a $150 doorbell price, the analogous landed-cost ceiling is about **$90**.
5. **Launch narrow.** Qualify one PoE outdoor camera and one wired doorbell.
   Leave the Wi-Fi slot empty until an exact current product clears media,
   secure commissioning, cloud isolation, price, and supply gates.

## Honest buyer guidance

- Buy **Ring** if the priority is the most familiar turnkey app experience and
  promotional entry price, and cloud/account dependence plus the recording
  subscription are acceptable.
- Buy **Wyze Cam v4** if the priority is inexpensive plug-in Wi-Fi coverage and
  per-camera microSD recording, and a vendor app/account with manually reviewed
  no-plan footage is acceptable.
- Buy the **Reolink PoE kit** if a complete local IP-camera/NVR system is needed
  today and a vendor-specific appliance experience is acceptable. This is the
  benchmark OSE must beat most directly.
- Buy the **ANNKE 3K Lite kit** if maximum fixed-camera count per dollar matters
  more than independent IP endpoints, high included-camera resolution, open
  integration, or easy camera relocation.
- Buy **OSE** only after the shipped system proves that owner-controlled local
  operation, a unified event experience, open implementation, repairability,
  and support justify its premium. Until then, OSE is a promise under test,
  not the automatically superior choice.

## Evidence limits and next checks

- Ring and Wyze prices/features above are current manufacturer-published facts;
  three-year totals and positioning conclusions are OSE calculations.
- The Reolink and ANNKE Amazon list prices were observed by the owner and are
  sourcing leads, not locked offers. ANNKE's direct page supplies the current
  exact-SKU price used in the total.
- No vendor-published claim proves first-frame time, notification delivery,
  night image, local recovery, disableable P2P, secure commissioning, or a
  shipping hardware revision. Those stay unverified until physical testing.
- Recheck exact SKU, seller, bundle contents, price, and return terms at sample
  approval. Do not use a sponsored search card or visually similar substitute.
