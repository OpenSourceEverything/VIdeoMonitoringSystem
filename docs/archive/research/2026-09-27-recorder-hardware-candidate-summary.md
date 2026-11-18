> Historical reference. [Current documentation](../../README.md) takes precedence.

# Recorder hardware candidate summary

Status: historical analysis, not the current purchase recommendation.
Current requirements, candidates and source reviews start at
../../research/README.md. The conclusions and prices below are dated;
neither the old controller price cap nor this benchmark order governs sourcing.

The supplied recorder report is retained privately. It contained three
concatenated research responses recommending different shortlists. This summary
records their reconciliation against the then-current requirements: eight
qualified cameras, a credible sixteen-camera architecture envelope, hardware video decode and inference,
normal Linux maintenance, no OSE SaaS, and minimum product-specific porting.

## Short answer

There are **three recorder paths worth benchmarking**. Only the first two are
finished off-the-shelf mini-PCs:

1. **ASUS NUC 14 Pro, Core Ultra 5 125H -- primary.** This is the safest first
   qualification host. It has standard x86 Linux, Intel hardware H.264/H.265
   decode, OpenVINO-capable Intel hardware, dual-channel memory, NVMe, 2.5 GbE,
   and substantially more 8/16-camera headroom than N150.
2. **ASUS NUC 14 Essential, Intel N150 -- cost-floor screening candidate.** It
   is inexpensive, low power, and uses the same general Linux/Intel media and
   OpenVINO software direction. It may qualify for eight cameras, but it is not
   a credible sixteen-camera choice until physical testing proves otherwise.
3. **Raspberry Pi 5 8 GB plus AI HAT+ 26 TOPS -- open assembly/reference
   candidate.** Hailo inference is promising, but the Pi 5 host's H.264 decode,
   storage/accelerator topology, cooling, and enclosure remain explicit gates.
   This is not an off-the-shelf recorder appliance.

**OSE should ship Linux, not Windows.** Buy a barebone or erase any included
OS, install the OSE Linux appliance image, and expose only the OSE interface.
ASUS documents Linux as a supported operating-system family for these NUC
kits. A marketplace listing saying "Windows" or "no OS" does not determine the
OSE software stack.

## Recommendation

Benchmark the **NUC 14 Pro 125H first**. It is the least likely to waste camera,
Frigate, storage, and UI engineering time behind another marginal recorder.
Use at least 16 GB in two matched SO-DIMMs and NVMe storage; install the OSE
Linux stack and run the existing four-camera reject gate followed by the full
eight-camera 24-hour test and sixteen-stream architecture probe.

Benchmark N150 only as the likely lower-cost SKU after the workload is known to
pass on 125H. Benchmark Pi 5/Hailo if a buildable open recorder and lower
power/BOM justify the additional platform work. This order does not lock the
production recorder; it gives the software effort a non-marginal reference
machine first.

Do not buy a new Pi 4/Coral combination as the product direction. Frigate still
supports Coral but no longer recommends it for new installations. The existing
Pi 4 can remain a compatibility fixture. Defer RK3588/Jetson and anonymous
mini-PCs until a first-class Frigate platform fails cost or performance gates;
otherwise their BSP, driver, and mutable-BOM risk adds work without answering a
current question.

## Current retail sourcing spot check

Checked 2026-09-27. These are sample/bench signals, not proof of production
supply or a locked price.

| Candidate | Amazon | eBay | Alibaba | Procurement meaning |
| --- | --- | --- | --- | --- |
| ASUS NUC 14 Pro 125H (`NUC14RVHU5` family) | Exact US listing not confirmed | Exact barebones are listed, including [new around $500](https://www.ebay.com/itm/227412091066) and [used/refurbished stock](https://www.ebay.com/itm/127925676370) | Generic 125H mini-PCs exist, but the exact ASUS BOM was not confirmed | Easy to obtain for a bench; request an ASUS/distributor quote before any BOM decision |
| ASUS NUC 14 Essential N150 (`RNUC14MNK150000U`) | Search evidence exists, but an exact dependable US offer was not confirmed | Exact new barebones are listed around [$279--$289](https://www.ebay.com/itm/267429583421) | Many generic N150 boxes exist; components and cooling may change silently | Obtainable cost-floor sample; lock an exact ASUS SKU for comparable results |
| Pi 5 + official AI HAT+ 26 TOPS | Exact current offer not confirmed in this pass | Exact HAT listings exist, but observed stock included a sold listing | [Nominal official-HAT listings exist](https://www.alibaba.com/product-detail/Raspberry-Pi-Official-AI-Hat-13_1601623290440.html), with seller/price ambiguity | Prefer an authorized Raspberry Pi reseller such as [SparkFun](https://www.sparkfun.com/raspberry-pi-ai-hat-26-tops.html) for qualification |

Amazon, eBay, and Alibaba are useful for locating one bench unit. They do not
establish lifecycle, stable internals, 100/1,000-unit price, warranty handling,
or product supply. The production decision needs manufacturer/distributor RFQs
for the exact part number after the physical benchmark passes.

## Why the long report disagrees with itself

The imported report contains three independent conclusions:

- one selects 125H plus Pi 5/Hailo-8L;
- one selects N150 plus Pi 5/Hailo-8 26 TOPS; and
- one selects 125H plus the existing Pi 4/Coral.

The N150 recommendation optimizes initial price and an eight-camera inference
estimate. The 125H recommendation optimizes complete-system headroom and the
sixteen-camera envelope. Pi/Hailo optimizes dedicated inference and openness
but leaves host decode and storage integration unproven. The current product
requirements make 125H the correct primary *qualification host*, not a final
BOM declaration.

## Sources to retain

- [ASUS NUC 14 Pro technical specification](https://dlcdnets.asus.com/pub/ASUS/NUC/TPS/NUC14_Pro_ProPlus_TPS_1.6_20250415.pdf?model=NUC14RVHU5)
- [ASUS NUC 14 Essential specifications](https://www.asus.com/displays-desktops/nucs/nuc-mini-pcs/asus-nuc-14-essential/techspec/)
- [Raspberry Pi AI HAT+ product page](https://www.raspberrypi.com/products/ai-hat/)
- [Frigate recommended hardware](https://docs.frigate.video/frigate/hardware/)
- Current workload and inference acceptance: ../../requirements/requirements.md
