# Controller board manufacturing

Required by FAB-001 in ../../requirements/requirements.md:
available, commercially reusable production files for the complete compute board,
so OSE can initiate fabrication independently. Buying a preassembled version of
that design remains preferred. This is not a quote or purchase authorization.

controller-sources.json holds exact revisions, source URLs,
inspection observations and licensing gaps. This explains their significance;
it is not another candidate list.

## Independent production needs

- Editable schematic and PCB routing for the compute hardware, not just a
  carrier, schematic PDF, connector drawing or layout screenshot.
- Matching BOM with sourceable exact parts, stackup/impedance specifications,
  fabrication/drill outputs, assembly drawings and component placement.
- Firmware/PMIC programming, boot provisioning and production-test instructions.
- An explicit hardware license permitting the intended commercial reproduction
  and modification. Absent or conflicting statements remain unresolved.
- Separately resolved software redistribution, branding and finished-product
  compliance. Published board files do not make proprietary runtimes open.

## Inspected file coverage

- Radxa CM5: native layout/BOM exists for CM5 IO, the carrier. Complete module
  PCB/BOM and reproduction rights remain unestablished. Converted Altium files
  have an accuracy caveat. A self-built carrier still needs purchased modules.
- ROCK 4D: schematic/placement PDFs and mechanical files are published; native
  production files and commercial reproduction rights were not established.
- Seeed reComputer RK3576: a 38-page compute-board schematic includes SoC,
  memory and power circuits. Native PCB/BOM/fabrication outputs and commercial
  reproduction rights remain unestablished. The separate module kit's carrier
  files are not the complete compute board.
- Luckfox Omni3576: module-plus-carrier hardware with published schematic and
  mechanical resources. A complete native compute-module PCB/BOM/assembly
  package and commercial reproduction license have not been established.
- NanoPi M5: manufacturer lists LPDDR4X/LPDDR5 schematic PDFs and a DXF archive
  as its PCB CAD download. Neither that listing nor open-source branding
  establishes routed PCB sources, production outputs, matched BOM and license.
- BeagleY-AI: the pinned native schematic/PCB, BOM, Gerbers/drills, assembly
  files and license are now gathered in the dedicated BeagleYAIHardware
  repository. Its board-reference document owns the reusable file inventory,
  rights, RAM-part findings and manufacturing limitations. Hardware CC-BY-4.0
  and AM67A support for up to 8GB are established; stock hardware remains 4GB.
  An 8GB derivative, complete production-test package and qualified OSE/Frigate
  path are not established. This is not a production-qualified 8GB purchase.
  Reference: ../../../../BeagleYAIHardware/README.md
  Detail: ../../../../BeagleYAIHardware/docs/board-reference.md
- LCSC TaishanPi-3M: schematic/PCB links and batch costs are published. The
  checked prebuilt is 4GB/64GB; indexed commercial-use statements conflict and
  direct file access was unavailable.
- devmeb0 RK3576-SBC: PCBWay names CERN-OHL-P-2.0, but the inspected GitHub tree
  has a schematic PDF and layout pictures, not native PCB/BOM/Gerber files.
  No stocked exact 8GB assembly or production price was established.

## Supply and cost

Files are not a manufacturing sign-off or proof of lower cost. Price the actual
8GB configuration, fabrication, BGA assembly/inspection, programming, yield,
testing, housing, cooling, power, freight and duty. Confirm SoC/RAM/PMIC supply;
owning PCB files does not create component stock. Recording storage remains
separate. Missing costs, rights and manufacturing readiness stay UNKNOWN.
