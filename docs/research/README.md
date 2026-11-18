# Hardware research

Start with candidates/controller.md or candidates/camera.md. Each generated
report shows only that role's exact configurations, not permission to purchase.
One top-level bullet identifies one complete model/configuration. Every cost,
drawing, technical fact and vendor/search link belongs beneath that item; there
is no detached vendor appendix or unexplained status label. URLs are raw text,
not hidden behind Markdown labels. Prose wraps for source reading; URLs stay intact.

The report answers whether all requirements are met, whether complete production
files and commercial reproduction rights are established, and whether independent
fabrication is feasible. These are separate YES/NO/UNKNOWN answers, not synonyms.
Approximate prices retain their observation dates; missing costs and quantities
remain UNKNOWN. No new price, stock or hardware result is implied by regeneration.

## Where information belongs

- candidates/controller.json: controller configurations, dated prices, complete
  costs, mandatory gates, vendor/search parameters and rejection records.
- candidates/camera.json: the corresponding camera records, independently edited.
- candidates/camera-sources.json: camera claims,
  direct URLs, source reviews, specification excerpts and dated offers.
- candidates/controller-sources.json: controller claims,
  exact offers, Linux support and named RAM/manufacturing research.
- candidates/controller-ram.md: readable 8GB/16GB reasoning,
  calculations and limits of the available evidence.
- candidates/controller-manufacturing.md: complete-board
  versus carrier-only files, reproduction rights and fabrication gaps.
- historical-sourcing.json: 408 older inspected
  offers, three failed lookups and the former report, with its original checksum.
  This is dated research, not another shortlist or a source-watch input.
- candidates/controller-ram.md: public RAM reasoning with usable upstream source
  links. The original supplied transcript is retained privately; its provenance
  hashes and unverified-estimate caveat remain in candidates/controller-sources.json.

Keep exact facts and source reviews in the relevant role-specific JSON file.
Use Markdown for reasoning, not duplicate shopping lists. Research objects have
descriptive subject names, not an ever-growing updates array. Keep rejected
evidence so unsuitable offers are not reinvestigated without a reason.

## Edit, generate and lint

- Edit candidates/controller.json and controller-sources.json for controllers,
  or candidates/camera.json and camera-sources.json for cameras. One candidate
  identifies one exact configuration. Do not maintain a mixed candidate file.
- Each candidate's sourcing object contains full_product_name, manufacturer,
  manufacturer_part_number, search_terms and named vendors. Manufacturer,
  Amazon, Alibaba, eBay, DigiKey and Mouser are explicit even when no listing
  is found; additional vendors can be retained without a second candidate list.
  Each vendor records its search URL separately from its product URL, seller,
  seller SKU, configuration result, exact-option USD price or null, stock,
  check date, evidence references and limitations.
- report_details holds the included/missing parts, technical summary, complete
  fabrication-file scope/downloads/license/feasibility, manufacturing cost and
  quantity, per-vendor quantity/origin/warranty/package terms, and review date.
  Its matching evidence value lives in the role's source JSON. Null costs or
  unknown rights must not be promoted into zero cost, an available license or
  a complete production package. The fabrication answer must agree with the
  controller's mandatory fabrication gate; camera fabrication is informative,
  not an added camera requirement.
- Run ./tools/candidates render to validate both catalogs and regenerate
  candidates/controller.md and candidates/camera.md with raw purchase/search URLs.
  Use --catalog docs/research/candidates/controller.json or camera.json to render
  or lint just one role. No role's report includes the other role's candidates.
- Run ./tools/candidates lint to reject schema errors, source-role mismatches,
  missing claims, inconsistent values/costs/gates, damaged historical records
  or stale generated output.
- Both commands work offline through their wrapper, from any directory. They
  do not refresh offers, contact sellers or establish physical qualification.
- ./tools/check-sourcing reads only validated non-rejected candidates.
  Reachability or stock wording is not procurement or hardware approval.
  It still watches the selected primary buying URL, not every discovery link
  or another configuration; vendor metadata does not widen automated requests.

The schema is implemented in ../../src/video_monitoring_system/candidate_catalog.py.
Each role-specific source file has schema_version, role, description, claims and
named research objects. Historical sourcing is loaded and checked separately.

## Evidence and decision rules

- Maximum three catalog records per role. Rejected records are retained but
  not displayed or automatically watched; other investigations stay in research.
- Published values need matching direct HTTPS sources, dates and reviewed
  support. RECORDED preserves an earlier observation, not a fresh read.
  UNREVIEWED cannot establish a displayed fact. Source meaning requires actual
  review; URL reachability and a support label cannot establish truth.
- An unavailable price is a dated observation, not a buyable price. Preserve
  exact variants and missing completion parts; do not reuse a cheaper default
  configuration's price for an unselected 8GB/16GB option.
- Vendor EXACT means a documented configuration match, not production
  qualification or guaranteed stock. SELECT_OPTIONS, DIFFERENT_CONFIGURATION,
  UNVERIFIED and BLOCKED retain useful links without an exact price claim.
  NONE_FOUND means no listing was established in the bounded search; NOT_CHECKED
  means no check was performed. Neither proves market-wide unavailability.
  Search URLs are reproducible navigation, never purchase evidence. The generated
  report keeps these links and gaps under their candidate's vendor/search bullets;
  rejected records keep their complete vendor metadata in JSON only.
- PASS needs reviewed evidence, plus measurements for physical gates. Vendor
  specifications, TOPS, model-only benchmarks and empty web interfaces are not
  complete-system acceptance. Missing documentation stays UNKNOWN, not FAIL.
- A failed mandatory gate means REJECTED; all passing means QUALIFIED. Remaining
  documented leads are PARTIAL, otherwise UNKNOWN. No status authorizes a purchase.
- Missing costs are null, never zero. Include item, completion parts, freight
  and duty; a missing component keeps total UNKNOWN. Recording storage remains
  separately costed. The 40% target/50% maximum applies to cameras only.
  Controller price has no eligibility cap or ratio; keep any intended selling
  price unknown until established. Business costs and sustainable sales remain
  release decisions, not a disguised controller price ceiling.
- Rank controllers by documented complete cost, otherwise the explicitly
  incomplete priced subtotal; unpriced offers last. Do not rank an unavailable
  historical low as purchasable. Cameras retain qualification/cost ordering.
  Complete licensed compute-board production files are mandatory for controllers;
  carrier-only drawings and schematic PDFs cannot establish fabrication readiness.
  Requirements own all limits;
  their checksum binds the reviewed gate coverage, not a waiver or hardware PASS.

production-candidates.md is a navigation pointer, not a combined candidate list.
../archive/research/ remains dated background.
../design.md owns quantities/interfaces; ../requirements/requirements.md owns limits.
