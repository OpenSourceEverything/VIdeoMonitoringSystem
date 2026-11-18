> Archived 2026-10-27. Correction: a listing mentioning H.265 does not establish that H.264 is unavailable; that earlier exclusion below was unsupported. Current [sourcing](../../research/README.md) takes precedence.

# Price-locked hardware sourcing — 2026-10-27

Selection rule: an exact orderable recorder/camera SKU must satisfy every
mandatory requirement, including its share of the customer-price target. A
headline price is not a landed quote. No requirement is waived to make a
listing fit.

## Current price signals

| Device | Exact listing and observed price | Requirements evidence | Status |
| --- | --- | --- | --- |
| PoE camera | [4COVR / Shenzhen Guowei, model GW-D5GD54Y](https://www.alibaba.com/product-detail/4COVR-5Mp-PoE-Security-Camera-On_1601599907319.html): Alibaba search shows $38–$40, minimum 1. The localized item page showed €36.42 for 1–19, with shipping unavailable. | Listing claims 5MP, IP67, PoE, mic/speaker, H.264/H.265 and 100 ft/30 m hybrid lighting. The seller is Shenzhen Guowei, 4COVR's parent. Its [manufacturer 5MP BinSight datasheet](https://cdn.shopify.com/s/files/1/0824/3632/5626/files/BinSight_Series_5MP_Dual_Light_IR_Fixed_Lens_Turret_IP_PoE_Camera.pdf?v=1778314907) documents 5MP main/substreams, H.264/H.265/MJPEG, 30m illuminator, two-way audio, IP67, RTSP, browser access and ONVIF. Alibaba model GW-D5GD54Y is not mapped to this datasheet/SKU. | Best price lead. Resolve exact-model mapping, simultaneous plain-H.264 main/substreams, local credential/setup/update controls, and landed/reorder price. |
| PoE camera | [4COVR manufacturer SKU B0D7Q4814G](https://4covr.com/products/5mp-smart-dual-light-poe-turret-camera): $52.19; page showed 2 in stock. | Its linked manufacturer datasheet lists 5MP main/substreams, H.264/H.265/MJPEG, 30m IR illuminator, 15m color spotlight, two-way audio, IP67, RTSP, browser access and ONVIF. It specifies D-WDR and does not explicitly say both streams can run plain H.264 simultaneously. | Best exact manufacturer-documented model; price/supply and stream/local-control details remain open. |
| PoE camera | [4COVR manufacturer SKU B0D7Q4DHVL](https://4covr.com/products/5mp-poe-waterproof-turret-camera): listed at $56.99; availability was inconsistent across page views. | 5MP, H.264/H.265 streams and 30m Smart IR are listed, but the specification explicitly says built-in microphone, no speaker, one-way audio. | Exclude: fails two-way audio. |
| Wi-Fi camera | [Alibaba 5MP Smart Wireless IP Camera](https://www.alibaba.com/product-detail/5MP-Smart-wireless-IP-Camera-with_1600111897100.html): $38, MOQ 1. | Listing claims 5MP, IP67 and two-way audio, but specifies H.265 only. Dual-band Wi-Fi, required H.264 streams and local setup/control are not established. | Exclude from this product: fails the documented H.264 requirement and lacks evidence for other mandatory interfaces. |
| Recorder | [Lingchuang N910 / Intel N100](https://www.alibaba.com/pla/New-2025-Mini-PC-with-16GB_1601617533891.html): listing headline says 16GB/512GB, $123, MOQ 1, one-year warranty. | The price/configuration headline is promising, but exact included PSU, Ethernet, storage expansion, Linux support, thermal limits and complete landed cost are not established. No evidence shows it sustains the required eight-camera decode/inference workload. | Price lead only; not selected. |

The Alibaba camera listing's $38–$40 would put four camera line items at
$152–$160 before freight, duty, tax or packaging. Adding the N910 headline gives
$275–$283 before a replaceable recording drive, PoE switching/cabling,
fulfillment, warranty and support. These are supplier price signals, not a
build cost or margin calculation. The customer targets remain $75–$100 per
standard camera, $150–$200 recorder compute with a $250 ceiling, and $600–$800
for a four-camera system.

## Decision

No exact recorder and camera combination in this sweep is evidenced to meet
the whole contract and price. Keep the price limits and product requirements
fixed. Pursue the 4COVR BinSight 5MP PoE turret as the current camera direction;
map the Alibaba configuration to its manufacturer datasheet and get sustainable
landed pricing before selection. 4COVR publishes a camera web/API [documentation
library](https://4covr.com/pages/download-center), but whether its local
interfaces cover every required setup/update operation remains open. The N910
is only a price-relevant recorder listing. Wi-Fi sourcing still has no exact
model meeting the required dual-band, 5MP H.264, two-way-audio, 30m IR and
local-control requirements.

Next sourcing action: resolve exact model identity and written per-stream codec,
night-range, local-control, Linux/recorder-interface, landed-price and warranty
details from source documents before putting any SKU in the BOM. No supplier
was contacted and no order was placed.
