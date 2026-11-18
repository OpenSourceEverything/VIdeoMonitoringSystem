> Historical reference. [Current documentation](../../README.md) takes precedence.

# Finished-camera OEM/ODM sourcing screen

Status: public desk research, checked 2026-09-29. No supplier was contacted,
no quote was requested, and no sample or inventory purchase is authorized by
this document. Every product statement below is a vendor claim until OSE
confirms it on a locked hardware and firmware revision.

**2026-10-03 owner override:** this is historical desk research, not the
current outreach list. Only exact, findable Amazon or Alibaba listings may
enter the next active owner shortlist. Mapesen has no verified exact listing
on either marketplace and is excluded. No vendor-only browsing, supplier
contact, or RFQ is the next action. The earlier RFQ language below records
what was considered, not current authorization.

## Historical owner answer (superseded by 2026-10-03 marketplace rule)

**CONDITIONAL GO -- prepare one common RFQ; do not order samples yet.**

- Do not design an OSE camera PCB for the first commercial offer.
- Use a finished retail camera only to prove the OSE integration and owner flow.
- In parallel, screen named manufacturers with existing finished camera designs.
- No supplier or model is qualified today.
- **$30--$40 fully landed at 500/1,000 units is a gate, not a known price.**
- Mapesen's `L2IT401LFA2-WT` / `L2IT601LFA2-WT` is the closest public feature
  match, but its Tuya/P2P path, local activation, security, certification,
  revision control, and price are unresolved.
- Longse provides a stronger conventional local-camera signal, but the exact
  `LIRDQFG400W` is 2.4-GHz-only and audio is optional.
- The formerly proposed next decision was whether to send an RFQ; this is now
  parked until an exact Amazon or Alibaba listing reopens a candidate.

## Why this is the KISS path

The differentiated product is the OSE recorder, setup, events, recovery, and
support experience. Buying an existing finished design avoids taking on image
sensor and lens engineering, RF layout, power electronics, weather sealing,
tooling, factory fixtures, and a new embedded Linux security program before
customers have proved demand.

White-labeling is not a shortcut around qualification. OSE would still own the
customer promise, incoming quality, firmware/security response, warranty, and
the accuracy of certification and product claims. A private enclosure or PCB
is reconsidered only after a qualified finished camera exposes a measured need
that cannot be solved by configuration or a locked existing design.

## Public candidate screen

| Manufacturer and exact public lead | Verified vendor-published fit | Missing evidence or known mismatch | Desk decision |
| --- | --- | --- | --- |
| **Mapesen `L2IT401LFA2-WT` / `L2IT601LFA2-WT`** | The [exact product page](https://mapesen.com/tuya-wifi-%26-4g-camera/l2it401lfa2-wt%EF%BD%9Cl2it601lfa2-wt.html) publishes 4/6 MP, H.264/H.265, main and substreams, dual-band 2.4/5 GHz Wi-Fi, RTSP, ONVIF, two-way audio, optional Ethernet, DC power, and IP66. Its [OEM/ODM page](https://us.mapesen.com/odmoem/) describes label, manual, packaging, logo, and private-mold services. | The listed platform uses Tuya and P2P. No public evidence proves OSE-controlled local activation, full cloud/P2P disablement, unique credentials, update signing, support lifetime, FCC ID for the exact radio/product, revision lock, MOQ, or landed price. | **RFQ lead 1; no sample yet.** Reject if local activation and permanent cloud disablement cannot be contractually and physically proved. |
| **Longse `LIRDQFG400W`** | The [exact manufacturer specification](https://www.longse.com/Products/NetworkProducts/NetworkCamera/WIFICAMERA/4MP/2022/0622/2257.html) publishes 4 MP, H.264/H.265, main and substreams, RTSP, ONVIF profiles S/T/G, Ethernet, IP67, DC power, and an exact model identity. Longse publishes an [OEM/ODM service policy](https://www.longse.com/Support/ServicePolicy/) and [two-year warranty policy](https://www.longse.com/Support/ServicePolicy/WarrantyRMA/). | This exact camera is 2.4-GHz-only, audio is optional, and vendor P2P is present. Local activation, credentials, P2P removal, security/update ownership, current quantity supply, certificates, revision control, and pricing are unproved. | **RFQ lead 2 for a dual-band/audio sibling; reject this exact model as the Wi-Fi default.** |
| **Sinovision `SN-IPV20-W1061DU`** | The [exact product page](https://www.sncctv.net/index.php/Product/view/id/1223.html) publishes H.264/H.265 dual streams, ONVIF, optional PoE, MOQ 100--500, 20--30-day volume lead time, 1--2-year warranty, and OEM/ODM options including firmware. | It is only 2 MP; its exact Wi-Fi bands are not established. The page asserts certifications but supplies no exact certificate IDs or reports. Local-only activation, RTSP behavior, security, revision lock, full cost, and supply identity remain unproved. | **RFQ lead 3 for an exact 4 MP+ dual-band successor, not a sample candidate.** |
| **Ansjer `C190 Pro` family** | The [manufacturer's OEM/ODM catalog](https://www.ansjer.com/wp-content/uploads/2024-Ansjer-Catalog-OEMODM.pdf) publishes a finished outdoor, dual-band, ONVIF/RTSP, two-way-audio, IP66 camera family. Ansjer also publishes an [OEM/ODM service page](https://www.ansjer.com/oem-odm/). | The published C190 Pro path is H.265-only, which fails the current browser/interoperability baseline. Local activation, cloud/P2P removal, security, FCC identity, pricing, supply, and revision controls are unproved. | **RFQ lead 4 only for an H.264-switchable exact sibling; reject the published H.265-only revision.** |
| **JideTech fixed outdoor sibling request** | JideTech publishes [OEM/ODM services](https://www.jidetech.com/pages/oem-odm-services) and an exact [P2 Wi-Fi PTZ](https://www.jidetech.com/products/p2-8mp-20x-wifi-ptz-camera-onvif-rtsp-rtmp) with H.264/H.265, RTSP/ONVIF, dual-band Wi-Fi, two-way audio, and IP66. | The exact public P2 is a large motorized 20x PTZ, not the simple fixed camera OSE needs. A matching fixed model, local setup, cloud/P2P removal, security, FCC identity, price, and revision lock are unproved. | **RFQ lead 5 for an existing fixed sibling only; do not customize the PTZ.** |

Camwon `WIP-TB600AF` remains a watch-list lead rather than an RFQ finalist. An
[exact marketplace-style product page](https://www.homewificam.com/vietnamese/sale-54865716-6mp-outdoor-wifi-camera-2-4ghz-5ghz-dual-band-security-camera.html)
publishes an attractive 4/6 MP, dual-band, H.264/H.265, ONVIF, audio, and IP66
combination, but OSE has not established a sufficiently strong direct
manufacturer identity and exact primary product record. It can replace a
finalist only after that provenance is independently verified.

The list is deliberately short. It is not evidence that these companies will
meet OSE's security or cost contract, and it is not permission to contact them.

## One common RFQ and evidence package

Send identical questions and require answers for one exact model, hardware
revision, firmware build, radio module, antenna, enclosure, and power supply.
Marketing answers without document identifiers do not close a gate.

### Commercial and supply

- Unit price at 1, 10, 100, 500, and 1,000 units; currency, Incoterm, payment
  terms, MOQ, sample cost, lead time, capacity, and quote validity.
- Every NRE, logo, label, manual, packaging, firmware, key-injection, mold,
  tooling, fixture, test, and certification charge.
- Exact BOM/revision lock, prohibited substitutions, product-change notice,
  last-time-buy terms, component origin, and supported production lifetime.
- Packaging dimensions/weight, freight route, tariff classification, country
  of origin, duties, insurance, and responsibility for damaged shipments.
- Factory quality plan, incoming/in-process/final tests, burn-in, yield and
  scrap history, sample traceability, and OSE audit rights.
- Warranty term, DOA/RMA procedure, return freight, repair/refurbish path,
  failure reporting, replacement stock, and spare-parts lifetime.

### Exact product behavior

- Native H.264 main stream at 1080p15 or better and simultaneous 360p--720p
  H.264 substream; independent FPS, bitrate, GOP/keyframe, and codec controls.
- Exact RTSP URLs and ONVIF profile/version/conformance record; snapshots,
  time sync, event metadata, microphone, speaker/two-way audio, IR/night mode,
  field of view, and latency.
- 2.4/5 GHz bands, supported channels/country controls, WPA2/WPA3 behavior,
  antenna type/gain, link RSSI/retry telemetry, Ethernet fallback, DC supply,
  power draw, temperatures, IP66/67 evidence, mount, and cable sealing.
- A stable physical/logical device identifier that survives DHCP, reboot,
  firmware update, reset, and re-enrollment without using IP or hostname as
  identity.

### Provisioning, privacy, and security

- Setup from factory reset without a vendor account, Internet, phone app,
  vendor cloud, vendor DNS, or inbound port forwarding.
- Documented local interface that lets OSE create unique credentials, transfer
  Wi-Fi securely, bind the device ID, change its network, and recover/reset it.
- A build with vendor P2P/cloud/telemetry absent or permanently disabled, plus
  a network manifest of every intended destination and protocol.
- Unique per-unit credentials or certificates; no shared/default production
  password. State who creates, injects, protects, transfers, rotates, revokes,
  and recovers every device key.
- Secure boot and debug-port policy; signed/authenticated updates, rollback,
  anti-downgrade, release notes, offline/local update, and recovery from a
  failed update.
- SBOM and open-source notices/source-offer procedure; vulnerability intake,
  CVE monitoring, severity SLA, fix delivery, support end date, and end-of-life
  owner behavior.

These questions operationalize the [NIST IoT device cybersecurity baseline](https://csrc.nist.gov/pubs/ir/8259/a/final):
unique device identification, authorized configuration, data protection,
restricted interfaces, secure updates, and security-state awareness. Passing
RTSP or ONVIF alone does not establish any of them.

### Compliance evidence

- Exact FCC ID, grantee, grant, operational description, internal/external
  photos, user manual, radio and RF-exposure reports, test lab, antenna list,
  and confirmation that the quoted build is electrically identical.
- Written identification of who is the US responsible party and who owns each
  recertification or permissive-change decision after firmware, antenna,
  enclosure, radio, model-number, or label changes.
- Exact reports for enclosure ingress/weather testing and the supplied AC/DC
  adapter's US safety and energy compliance; RoHS and California warning
  evidence where applicable.
- Manufacturer, affiliates, chipset, radio, and video-processing supply-chain
  screen against the current [FCC Covered List](https://docs.fcc.gov/public/attachments/DA-26-585A1.pdf),
  plus any customer-segment-specific NDAA or procurement requirements. This is
  a factual screen, not a legal conclusion.

OSE branding does not automatically invalidate an existing FCC authorization,
but it also does not automatically inherit it. FCC guidance permits different
trade names for an *identical* authorized product under the same FCC ID; a
change in design, circuitry, construction, radio behavior, antenna, or other
material configuration needs qualified compliance review. See the FCC's
[trade-name guidance](https://apps.fcc.gov/oetcf/kdb/forms/FTSSearchResultPage.cfm?id=20557&switch=P)
and [change-in-identification guidance](https://apps.fcc.gov/oetcf/kdb/forms/FTSSearchResultPage.cfm?id=188453&switch=P).

## Cost gate

The relevant comparison is not the factory's bare unit quote:

```text
fully landed camera cost =
    quoted camera and supplied accessories
  + inbound freight, insurance, duty and brokerage
  + OSE label, packaging and printed material
  + receiving inspection, programming, key handling and burn-in
  + expected scrap, DOA, return and warranty cost
  + certification, tooling and NRE amortization
  + carrying cost and replacement stock
```

The **$30--$40** target applies to that full result at a useful committed volume,
not to an advertised module or EXW camera price. At 10 units OSE should expect
prototype-like costs. A model advances only if a written quote supports the
complete OSE contribution model at 100/500/1,000 units and does not require an
unsafe commitment to reach the headline price.

## Staged decision and human/AI lock step

### Stage 0 -- public screen (complete)

- **AI does:** preserve direct product evidence, expose disqualifiers, prepare
  one RFQ, and make no contact.
- **Human does:** review the five names, contract, and permitted contact scope.
- **Success means:** the questions and stop conditions are accepted before any
  supplier receives OSE information.

### Stage 1 -- written RFQ (requires explicit authorization)

- **Human does:** approve outbound contact and the OSE identity/contact details
  to use.
- **AI does:** prepare or send the same approved RFQ to all finalists, record
  every answer as a vendor claim, and make no purchase commitment.
- **Success means:** at least two manufacturers return exact-model evidence,
  price breaks, revision terms, security answers, and certificate identifiers.

### Stage 2 -- evidence comparison

- **AI does:** verify certificate IDs and business identity, calculate full
  landed low/base/high cost, and rank gaps without filling them by inference.
- **Human does:** choose zero, one, or two exact samples and approve a total
  spend ceiling.
- **Success means:** a sample is ordered only to answer named physical gates;
  no sample is treated as a production commitment.

### Stage 3 -- physical acceptance

- **Human does:** power/reset/mount the exact units and perform the owner setup,
  outage, day/night, and walk-by actions prompted by the test plan.
- **AI does:** automate protocol, stream, network, performance, security,
  recovery, and 24-hour continuity measurements and retain the evidence.
- **Success means:** the exact revision passes the OSE camera contract without
  vendor cloud, shared credentials, recorder transcoding, or manual IP work.

### Stage 4 -- commercial gate

- **Human does:** approve any ten-unit pilot only after demand and cash-risk
  evidence is reviewed.
- **AI does:** reconcile the locked sample, quote, warranty, support load, and
  full contribution economics with the market-viability report.
- **Success means:** ten units are bought only when the exact sellable package
  has positive base-case contribution and a tolerable stated worst-case loss.

## Stop conditions

Stop an exact product if it requires a vendor account/cloud for activation or
normal local operation; cannot permanently disable vendor P2P; uses a shared
production credential; cannot supply authenticated updates and a supported
security lifecycle; silently substitutes hardware/firmware; lacks verifiable
US radio authorization for the quoted build; cannot deliver independent H.264
main/substreams; or misses the full landed-cost gate.

## Unresolved evidence

No candidate currently has a written OSE quote, verified landed cost, locked
BOM/firmware revision, certificate package matched to the quoted build, signed
security commitments, vulnerability/update SLA, verified local commissioning
interface, physical stream/recovery results, or measured customer willingness
to pay. Those absences are the reason for **CONDITIONAL GO**, not details to be
filled in later.
