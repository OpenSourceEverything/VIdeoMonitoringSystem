# Current state

Updated: 2026-11-10. Controller price is ranking-only, 8GB is the RAM minimum,
and complete licensed compute-board production files are mandatory.
Pilot measurements remain dated 2026-10-30.

Build a local Linux recorder with finished outdoor cameras. The single
[requirements document](requirements/requirements.md) governs the saleable system.
Read research/candidates/controller.md or research/candidates/camera.md.
Each has matching .json data and a separate controller-sources.json or
camera-sources.json in that directory. The
[research index](research/README.md) explains the files.
No exact camera, controller or complete kit is production-approved.
The old Pi installation is a historical pilot, not saleable hardware.

## Next actions

Controller fabrication requirement: available licensed native production files
permitting OSE to manufacture the complete compute board or a derivative.
A preassembled version of that design is preferred. The requirement distinguishes
full compute hardware from carrier-only files.
The manufacturing research (research/candidates/controller-manufacturing.md) separates
complete compute-board files from carrier-only files. No current controller has
verified full file coverage and commercial reproduction rights.

1. **Rank exact 8GB-and-up SoC assemblies by price and resolve fabrication files.**
   Preserve the complete eight-camera workload, concurrency, reliability
   and resource limits; do not reduce functionality to fit a cheaper board.
   The RAM research (research/candidates/controller-ram.md) separates upstream guidance,
   calculated frame/cache allocations and the still-missing complete peak budget.
   The fixed 16GB requirement has been replaced by an 8GB minimum. Exact offers and source
   limitations are in controller-sources.json; none establishes a complete eligible
   purchase. Seeed RK3576-30 is a concrete 8GB enclosed lead at $199 before delivery,
   with no controller price ceiling; stock and price are seller reports,
   not capacity or fabrication qualification. Unavailable ROCK 4D/CM5 offers do not
   establish global unavailability or a market-wide price.
2. **Disclose complete costs without a controller price rejection.** SRC-001
   through SRC-005 retain the 40% target/50% maximum for cameras only.
   Controller purchase/selling prices and ratios have no eligibility limit.
   A retail offer is not automatically production supply.
   Include missing power/accessories, freight/duty, assembly/test and support
   economics; unknown charges are not zero.
3. **Close exact hardware and camera interface gaps internally.** Establish
   maintained Linux decode AND inference on the exact controller stack.
   Rockchip support documentation and model-only benchmarks establish a path,
   not eight-camera capacity. Pin compatible drivers/runtime/models; package
   models for offline first setup and resolve binary licensing, signed updates
   and recovery. Do not begin a Yocto/Buildroot migration or software port merely
   because the board vendor publishes those build paths.
   Verify generic local camera setup, concurrent plain H.264 main/substreams,
   sustained FPS, direct talk, reset and firmware/security evidence. No new
   supplier email is the next dependency.
4. **Keep acquisition separate from research.** The prior single-camera
   research proposal is still unapproved, not a requirement waiver or a
   production-development platform. The proposed single C500P I51HJ research
   sample, capped at $100 delivered with needed PoE/cables/tax, still needs explicit
   owner acceptance of the exception and purchase. No four-pack is recommended.
5. **Develop only on eligible, approved hardware, then qualify the system.**
   Implement verified adapters and storage failure/restore handling, then run
   the eight-camera/24-hour acceptance. A one-camera test, CPU name, NPU TOPS
   rating or generic Linux claim is not a product-capacity result.

Camera pricing, security, local-only lifecycle and physical system acceptance
are unchanged. License/redistribution, repeatable supply, warranty,
support and sustainable complete-kit economics remain release gates.
No vendor contact, purchase, board access, deployment, commit or push occurred.
The November 6 Desktop packet remains a dated snapshot, not the maintained list.

## Sourcing and automation status

- Edit research/candidates/controller.json or camera.json and its matching
  controller-sources.json or camera-sources.json, then run ./tools/candidates render.
  ./tools/candidates lint checks schema, support reviews, costs, gates and stale output.
- The read-only sourcing checker reads only the validated active JSON records.
  Preserved offer/watch identities are historical evidence, not approval.
- Bounded Amazon/Alibaba/eBay coverage and lookup failures remain recorded
  in historical sourcing (research/historical-sourcing.json) and named
  source research. A failed read is UNKNOWN, not proof that hardware fails.
- Distributor adapters remain in marketplace-automation; credential setup and
  live API verification are separate from this consolidation.
- Finished cameras remain the default. Controller board-plus-housing research
  does not authorize custom camera boards, a Yocto migration or legacy Pi tuning.

## Latest pilot FPS check - 2026-10-30

The test recorder was a four-core Pi 4 with active recorder and media-engine
services. Camera labels below are anonymized. Installation locations and
access/recovery status are retained privately; these are dated measurements,
not a fresh inspection of an installation.

A read-only 60-second Frigate sample covered three engine-managed cameras.
During its higher-load window, processed rates averaged 4.72 (Camera B),
4.95 (Camera A) and 3.35 (Camera C), with 1.69 skipped FPS on Camera C.
A quieter repeat processed about 5 FPS on all three. Host CPU averaged 54% in
the quieter sample (peaking at 85% on a 5-second interval) and 83% in the
higher-load sample. That leaves less than 30% CPU headroom during load.

Recorder-ingress H.264 streams measured about 5.7-8.0 FPS; six saved segments
per camera measured 6.4-7.2 FPS, in the same range. The incoming stream already
falls short of its profile; these separate measurements do not prove where
individual frames were lost. Camera health reports a configured 1920x1080, 15
FPS profile, not sensor-level throughput.
Browser View FPS and playback were not measured. The cameras are about 2 MP,
below the 5 MP product minimum.
An additional bench unit reported online but was not bound to Frigate and is
excluded from the sample. Full details:
archive/diagnostics/2026-10-30-pi-fps-diagnostic.md.

Do not tune the legacy Pi or weaken requirements. Continue exact-hardware
sourcing, then repeat source, saved-video, detection, browser and capacity tests
on hardware that meets the [product requirements](requirements/requirements.md).

## Measurement limits

An online camera-health response does not prove media-engine binding or recording.
Notification provider acceptance does not establish locked-phone display or
end-to-end arrival time. Those measurements remain separate acceptance work;
subscriber enrollment and installation state are private.
See design.md#camera-onboarding and design.md#notifications for the maintained
onboarding and notification behavior.

[Design](design.md) is the single current architecture and interface document.
[Archive](archive/README.md) retains dated research, reviews and measured evidence,
not intermediate requirements/design copies. This page is the only current action list.
