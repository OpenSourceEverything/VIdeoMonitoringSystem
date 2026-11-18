> Archived 2026-10-27. Prices and status are historical. Current [sourcing](../../research/README.md) takes precedence.

# Hardware check - 2026-10-20

No order approved. Public Amazon and Alibaba listing claims below were read
through marketplace inspection on this date, not checkout. Seller, price and
availability can change. No coupons, tax or shipping are assumed. Account-free
Alibaba exact searches and eBay searches were blocked; see the automation
results below.

## Recorder listings

| Exact listing | Observed model/configuration | Price / seller claim | Assessment |
| --- | --- | --- | --- |
| [Beelink Mini S12 Pro - B0BVFS94J5](https://www.amazon.com/dp/B0BVFS94J5) | N100, 16 GB, 500 GB | $359.00 / Beelink-XXY; in-stock claim | Relevant architecture, above $250 compute ceiling; Linux/load not qualified. |
| [Beelink Mini S13 - B0DP2SGVVY](https://www.amazon.com/dp/B0DP2SGVVY) | N150, 16 GB, 500 GB | $399.00 / Beelink USA; in-stock claim | Same hold; paying more is not proof of better detection capacity. |
| [Bmax N150 - B0F6TK259N](https://www.amazon.com/dp/B0F6TK259N) | N150, 16 GB/512 GB | $280.79 / J-Tech Digital; in-stock claim | Lowest exact Amazon N150 configuration found in this sweep; still above $250 and not workload-qualified. |
| [Beelink Mini S12 Pro - B0GY42SQ3L](https://www.amazon.com/dp/B0GY42SQ3L) | N100, 16 GB/512 GB | $339.00 / Beelink-shop; in-stock claim | Exact configuration, above $250 and not workload-qualified. |
| [GMKtec - B0CH81C4K3](https://www.amazon.com/dp/B0CH81C4K3) | G11 Plus, Ryzen 5 3500U, 16/512 GB | $339.99 / GMKtec-US; availability unknown | Search returned it for N100/N150, but exact page is a different CPU. Reject that match. |
| [GMKtec - B0GHDTHM53](https://www.amazon.com/dp/B0GHDTHM53) | G3 Ultra, Pentium Gold 7505, 16/512 GB | $309.99 / GMKtec-US; availability unknown | Another wrong-CPU search result. Not an N100/N150 price. |

## Camera listings

| Exact listing | Observed model/quantity | Price / seller claim | Assessment |
| --- | --- | --- | --- |
| [Reolink RLC-510WA - B08PYN7TS2](https://www.amazon.com/dp/B08PYN7TS2) | One 5 MP dual-band Wi-Fi camera | $74.99 / ReolinkDirect; in-stock claim | Price-relevant, but microphone-only is not required two-way audio. Local control/setup still unqualified. |
| [Reolink RLC-520A two-pack - B09MCSCRGV](https://www.amazon.com/dp/B09MCSCRGV) | Two 5 MP PoE cameras | $134.99 / ReolinkDirect; in-stock claim | Not an RLC-540A despite that search query. Same two-way-audio shortfall. |
| [Reolink Lumus - B08628J862](https://www.amazon.com/dp/B08628J862) | One 4 MP dual-band Wi-Fi camera; two-way claim | $69.99 / ReolinkDirect; in-stock claim | Fails the subsequently approved 5 MP minimum; do not purchase under the current requirements. Local interfaces also remain unqualified. |
| [Reolink bundle - B0HD6L1DCF](https://www.amazon.com/dp/B0HD6L1DCF) | RLC-810WA plus indoor E1 Pro | $175.48 / ReolinkDirect; in-stock claim | Not two outdoor 5 MP cameras. Full-resolution H.264 and setup remain unproved. |

Do not treat search matches as passing the requirements. Inspection caught model,
quantity and CPU mismatches. Missing evidence means unknown, not passed.

## Technical leads, not sourceable approved units

Vendor specification claims checked 2026-10-20:

- [RLC-511WA](https://reolink.com/us/product/rlc-511wa/): 5 MP H.264,
  main/substreams, dual-band Wi-Fi, two-way audio and IP67. A matching current
  Amazon/Alibaba/eBay listing was not established in this check. Do not
  substitute the 510WA. Local activation/control needs separate proof.
- [RLC-540A specification PDF](https://m.media-amazon.com/images/I/61fsTXAZWhL.pdf):
  5 MP PoE, H.264/H.265 and two-way audio. Codec names alone do not prove
  simultaneous plain-H.264 main/substreams at the required resolution. A
  matching current listing and exact firmware remain unverified.
- [Reolink two-way-audio model list](https://support.reolink.com/articles/360003764334-Which-Reolink-Cameras-Support-Two-Way-Audio/)
  distinguishes the speaker-equipped models; do not infer talk from a microphone.
- [Wi-Fi camera initial setup](https://support.reolink.com/articles/7983219944985-How-to-Initially-Set-up-Reolink-WiFi-Cameras-With-LAN-Port-and-Voice-Prompt/)
  documents vendor App/Client setup, not a qualified vendor-free commissioning API.

## Conclusion

The platform direction remains N100/N150-class recording plus finished native
H.264 cameras. No inspected combination currently passes every price, interface
and qualification gate. That is a purchasing blocker, not evidence that local
recording cannot work. Keep the strict sold-kit contract; a disclosed BYOC
compatibility exception requires explicit approval, not silent relaxation.

After this listing check, the owner approved the camera proposal. The
[camera requirements](../../requirements/requirements.md#cameras) now replace the old
1080p floor; the required H.264 main stream must meet the new resolution floor.
Re-screen candidates against the full approved contract before purchase.
Higher pixel count alone does not establish moving-person quality at night.
Sensor size, lighting, exposure and full-resolution codec behavior need actual tests.

## Marketplace automation sweep - 2026-10-20

Public listing/search pages were checked through the marketplace tools. These
are leads only; titles and prices do not prove the requirements.

| Exact listing | Public page claim | Price claim | Current result |
| --- | --- | ---: | --- |
| [ENSTER 5 MP Wi-Fi PTZ - B0GVMYWJFT](https://www.amazon.com/dp/B0GVMYWJFT) | ONVIF/RTSP, two-way audio, IP66 | $89.99 | H.264 streams, sustained FPS, HDR/WDR, local first-time setup and exact firmware not verified; does not clear the requirements screen. |
| [JideTech 5 MP Wi-Fi PTZ - B0FL7TRY82](https://www.amazon.com/dp/B0FL7TRY82) | Dual-band Wi-Fi, IP66, NVR compatible | $47.37 | Codec, ONVIF/RTSP, two-way audio and local setup not established. Do not count as compatible. |
| [Anpviz 5 MP PoE - B0FKG25JVC](https://www.amazon.com/dp/B0FKG25JVC) | Mic, requires NVR, not standalone | $34.49 | Fails required two-way audio; exclude. |
| [Anpviz 5 MP PoE dome - B07N3Y4HS6](https://www.amazon.com/dp/B07N3Y4HS6) | H.265, IP66, 24/7 recording | $51.99 | Required plain-H.264 main/substreams and two-way audio not established; exclude pending proof. |
| [Anpviz 5 MP PoE dome - B07N3ZBX2N](https://www.amazon.com/dp/B07N3ZBX2N) | Mic/audio, IP66 | $34.99 | Microphone is not two-way audio; exclude. |
| [Alibaba 5 MP turret](https://www.alibaba.com/product-detail/Smart-Dual-Light-Lite-AI-5MP_1601924054929.html) | 5 MP dual-light turret; Hangzhou Zanuo | $54 at 1; $52 at 100 | Price tier is not landed cost. Codec, streams, audio, IP rating, exact model and local setup remain unknown. |
| [ANNKE FCD600 6 MP PoE](https://www.alibaba.com/product-detail/ANNKE-FCD600-6MP-PoE-IP-Dual_1601417491893.html) | Dual lens, PoE, two-way audio | $82 at 1 | Too little evidence for codec, local setup, HDR/WDR and exact firmware; not qualified. |
| [Alibaba N1 mini PC](https://www.alibaba.com/product-detail/Factory-N1-Intel-N95-N100-N150_1601805126433.html) | Listing title combines N95/N100/N150, 16 GB/512 GB | $179 at 2; $165 at 50 | Processor variant is ambiguous. Price, Linux support and complete configuration need exact-option confirmation. |
| [Alibaba fanless N100/N150 PC](https://www.alibaba.com/product-detail/12th-Gen-Intel-N100-Fanless-Linux_1601522877188.html) | N100/N150 title; Linux | $106 at 1; $102 at 200 | Seller, processor option, RAM, storage, PSU, shipping and thermals unknown; not a complete-recorder price. |
| [Beelink Mini S12 Pro - B0D5QXTFHH](https://www.amazon.com/dp/B0D5QXTFHH) | N100, 16 GB/512 GB | $349 | Identified configuration, but above the $250 compute planning ceiling and not workload-qualified. |

The search automation returned public product links for Amazon and Alibaba.
The account-free eBay search returned HTTP 403 in both the direct and rendered
browser paths; no results were available and no verification challenge was
bypassed. eBay candidate coverage therefore remains incomplete.

**Result:** no purchase-ready camera or recorder. The cheapest listings fail a
known requirement or lack evidence for it. The next sourcing gate is exact
model/firmware proof for plain-H.264 main and substreams, required audio and
local setup, plus a recorder with an unambiguous CPU/RAM/storage configuration.

## Exact Reolink marketplace follow-up - 2026-10-20

Searches for RLC-511WA, RLC-523WA, RLC-823A and RLC-843A did not surface those
exact models on Alibaba or eBay; those pages were blocked by verification/403.
Amazon returned these exact, inspectable Reolink listings:

| Exact listing | Public page claim | Price claim | Gate |
| --- | --- | ---: | --- |
| [RLC-811WA - B0CDPX99X2](https://www.amazon.com/dp/B0CDPX99X2) | 8 MP Wi-Fi, two-way audio, H.265 | $159.99 | Reolink's [RTSP codec note](https://support.reolink.com/articles/900000638523-What-s-the-Format-of-the-RTSP-Video-Audio-that-Reolink-Cameras-Use/) says 8 MP main stream is H.265 and substream H.264. Exclude while H.264 main is mandatory. |
| [RLC-810WA - B0CDPXD4BT](https://www.amazon.com/dp/B0CDPXD4BT) | 8 MP Wi-Fi, two-way talk, IP67, H.265 | $129.99 | Same H.264-main issue; exclude pending a supported setting that produces both required H.264 streams. |
| [TrackMix Wi-Fi - B0B4SB2CRD](https://www.amazon.com/dp/B0B4SB2CRD) | 8 MP dual-lens Wi-Fi PTZ | $169.99 | Codec and exact two-way control not proven in the listing; 8 MP RTSP main-stream issue remains. |
| [TrackMix PoE - B0B4S6F749](https://www.amazon.com/dp/B0B4S6F749) | 8 MP dual-lens PoE PTZ | $169.99 | Same H.264-main issue; price exceeds the $75-100 camera planning range. |
| [RLC-840A - B0CM391HDK](https://www.amazon.com/dp/B0CM391HDK) | 8 MP PoE turret, color night light | $119.99 | H.264 main-stream behavior and exact local two-way control unverified; over the planning range. |
| [RLC-1224A - B08LKL4TR6](https://www.amazon.com/dp/B08LKL4TR6) | 12 MP PoE, two-way talk, 24/7 recording | $129.99 | Reolink [specifies 20 FPS, 30 m IR, two-way audio, H.264/H.265 and RTSP](https://reolink.com/ca/product/rlc-1224a/), but its RTSP main/substream codec combination still needs exact proof; over the camera planning range. |

The Wi-Fi gap remains: the exact Reolink RLC-511WA has vendor-documented
5 MP H.264, dual-band Wi-Fi, two-way audio, 30 m IR and IP67, but the marketplace
search did not find a matching Amazon/Alibaba/eBay offer. The similar RLC-510WA
has only a microphone and fails two-way audio. See Reolink's [model comparison](https://reolink.com/files/docs/specs/reolink_product_family.pdf).

## Best current marketplace leads

One further Amazon search found a lower-priced PoE listing and a possible
Wi-Fi listing. Neither clears the requirements screen:

| Exact listing | Public claims and price | Missing proof |
| --- | --- | --- |
| [Amcrest AMLINK AL5M-T5171EW - B0GW13FT4F](https://www.amazon.com/dp/B0GW13FT4F) | $79.99 / Amcrest Direct. Exact 5 MP PoE model; manufacturer claims 20 FPS, two-way audio, IP67, 30 m IR, and H.264/H.265. | Manufacturer pages disagree on codec wording; simultaneous H.264 main/substreams, local talk, local first setup, WDR and night facial detail remain unverified. It does not clear the requirements screen. [Manufacturer specs](https://amcrest.com/products/amlink-5mp-security-ai-ip-poe-turret-camera-al5m-t5171ew) |
| [ENSTER 5 MP Wi-Fi PTZ - B0GVMYWJFT](https://www.amazon.com/dp/B0GVMYWJFT) | $89.99 / ENSTER. Listing title claims ONVIF/RTSP, two-way audio and IP66. | H.264 main/substreams, local first setup, credentials, update path, WDR and real night performance remain unverified; it does not clear the requirements screen. |

The Amcrest page links to a matching Amazon listing and publishes the exact
model. It is a stronger lead than an unbranded result, but its manual/spec
files require further review. The $79.99 Amazon claim is not a landed quote.

This sweep exposes a product gate: popular 8 MP Reolink cameras use H.265 for
the RTSP main stream, while OSE requires H.264 main and detection streams. Do
not assume a vendor page listing both codecs proves that both streams can run
H.264 simultaneously. Current eBay account-free search is unusable; eBay's
[Browse API](https://developer.ebay.com/develop/api/buy) supports listing search
with an application access token, so a durable integration needs eBay developer
credentials held locally as secrets. No credentials or account changes were
made.
