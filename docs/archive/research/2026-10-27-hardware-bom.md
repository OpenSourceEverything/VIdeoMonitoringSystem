> Archived 2026-10-27. Current [device requirements](../../requirements/requirements.md) and [sourcing](../../research/README.md) take precedence.

# Hardware and BOM

Decision: use finished cameras and a Linux mini PC. Do not design camera boards.
Every exact SKU must meet all documented requirements, including recorder
capacity and customer-price limits. Brand or processor family is not approval.

Wi-Fi carries video wirelessly but still needs camera power. PoE carries both
power and video over one Ethernet cable. Both connect to the same recorder.

## Four-camera parts list

Observed price signals, not landed costs, approved SKUs or complete-system
prices. The camera mix satisfies
`Wi-Fi quantity + PoE quantity = 4`.

| Part | Quantity | Selection | Observed cost signal |
| --- | ---: | --- | ---: |
| Recorder with PSU, RAM and boot SSD | 1 | Exact 16 GB+ Linux configuration with wired Ethernet and supported decode/inference | Alibaba Lingchuang N910 N100 16/512 headline: $123, MOQ 1; exact configuration, interfaces and landed price remain unclear |
| Finished outdoor cameras | 4 total | Exact 5MP PoE model with two-way audio, 30m IR and local controls | Alibaba 4COVR GW-D5GD54Y listing: $38-40 each, MOQ 1; exact-model codec, local-control, IR and shipping evidence are incomplete |
| Recording drive | 1 | Replaceable, endurance-rated; 2 TB planning size | $100-160 |
| Mounts, weatherproof connections, Ethernet uplink, power accessories | 1 set | Include missing items; do not double-count camera-box contents | $30-70 |
| PoE switch and camera cable/glands | If any PoE cameras | Standards-compliant ports and measured power budget for the selected cameras | Add $60-140 |
| Home Wi-Fi/mesh coverage and camera power outlets | If any Wi-Fi cameras | Verify all locations before installation | Existing; upgrades unpriced |

Four 4COVR Alibaba camera price signals plus the N910 headline total $275-283
before shipping, tax, storage, switching, cables, support or warranty. This is
not a landed BOM. Current price leads and their missing requirements evidence
are in the [2026-10-27 sourcing check](../research/2026-10-27-price-locked-sourcing.md).

These totals omit installation, labor, returns, warranty and support. They do
not establish sustainable retail economics or prove four-camera capacity.

Optional UPS, additional network coverage and longer cable runs require separate
pricing. An existing recording drive may be reused for a short test after health
checks; do not erase it or assume it covers the full retention promise.

## Storage sizing

Decimal GB/day = camera count x main-stream Mbps x 10.8.
Required usable capacity = GB/day x retention days / 0.8, reserving 20%.

Four cameras at 4 Mbps need about 1.51 TB for seven continuous days before audio
and other overhead. A 2 TB drive is the planning baseline, subject to measured
bitrate and actual usable capacity. Do not size storage from event frequency.
The approved higher-resolution camera contract requires rechecking bitrate,
storage, recorder load and cost. Do not reduce image quality to fit this estimate.

## Unresolved hardware requirements

| Item | Direction | Why not approved yet? |
| --- | --- | --- |
| Recorder | Exact affordable Linux mini PC | Must meet eight-camera decode/inference, thermal, RAM, storage and price requirements as one exact configuration. |
| Wi-Fi camera | No exact model selected | Must meet dual-band, H.264 main/substreams, two-way audio, night range, weather and local-control requirements within the camera price target. |
| PoE camera | 4COVR GW-D5GD54Y is the current Alibaba price lead | Exact SKU mapping, simultaneous plain-H.264 streams, local controls, 30m IR and landed cost must be established. 4COVR models with documented one-way audio are excluded. |

Select only a complete, returnable SKU that meets the price and interface gates.
The selected recorder must also pass the [capacity requirement](../../requirements/requirements.md#performance-and-capacity).

## Purchase gate

Record the exact SKU, hardware revision, firmware, seller, condition, included
PSU/storage/mounts, current price, shipping, return terms and total. Verify
the [camera](../../requirements/requirements.md#cameras) and
[recorder](../../requirements/requirements.md#controller-recorder) requirements without assuming a
brand's other models behave alike. Any exception needs explicit approval and
must not be advertised as meeting the sold-product contract.
