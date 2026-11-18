# Controller RAM: 8GB versus 16GB

The controller requirements (../../requirements/requirements.md) allow 8GB or
more, subject to the unchanged eight-camera workload, concurrency, reliability
and resource limits. 16GB is no longer a fixed minimum. A smaller RAM option
does not itself demonstrate the complete workload or justify removing features.

Exact sources, offers and report provenance live in
controller-sources.json, under its named RAM/CM5/8GB subjects.
This explains the reasoning; it is not a second hardware shortlist.

## Workload

- Copy eight native compressed main streams without routine transcoding.
- Process five detection frames per second per camera from smaller substreams,
  with under 1% skipped frames, alongside viewing, alerts, playback and export.
- Preserve required CPU/inference reserve, no throttling, recovery and 24-hour
  acceptance. Lower RAM does not justify removing features.

See ../../requirements/requirements.md. Recording, decoding and
neural-network inference are separate workloads; an NPU's TOPS rating does not
determine memory use or complete product capacity.

## Why the report favored 16GB

An earlier supplied report, retained privately, argued
for playback/export, reconnect, storage-stall and software-growth reserve. Its
4.5–8.2GB working range was estimated, not a measured OSE peak. Its bounded offer
search did not establish a market-wide minimum price or prove 8GB unsaleable.

Frigate recommends 16GB: https://docs.frigate.video/frigate/planning_setup/#ram-memory
for many-camera/enrichment-heavy use, not as a universal minimum. Do not add
optional enrichment models merely to justify more RAM.

## Calculable memory components

The upstream shared-memory calculator:
https://docs.frigate.video/frigate/installation/#calculating-required-shm-size

It gives about 444 MiB for eight 1280x720 detection streams plus room for two added
cameras, at 25 frames each, including 50 MiB for logs/cache. Its per-camera formula
is (width × height × 1.5 + 270480) ÷ 1048576 × frames.
At 50 frames, the corresponding total allocation is about 838 MiB.

An example 1,000,000,000-byte recording cache adds about 954 MiB. Together these
are approximately 1.37–1.75 GiB, not the full machine footprint. These calculations
use stated settings; they are neither measurements nor new acceptance limits.
Shared-memory size is a limit, not proof that all of it is resident.

Still account for Linux/services, decoder surfaces, Frigate processes/models,
accelerator allocations, OSE UI/database, concurrent playback/export, reconnects,
storage stalls and reserve. Do not double-count shared mappings by summing every
process's RSS. Establish usable RAM and hardware reservations for the exact image.
Swap is not a substitute for processing deadlines.

## Decision

8GB is a credible design direction, not a complete eight-camera PASS. The known
frame/cache allocations do not themselves require 16GB; the remaining peak-memory
budget and supported software path need resolution. Keep capacity and quality
criteria unchanged. Do not buy a reference machine solely to answer this question
or substitute 4GB hardware because its board files are more open.
