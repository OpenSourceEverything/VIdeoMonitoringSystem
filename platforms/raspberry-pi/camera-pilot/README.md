# Raspberry Pi camera pilot adapters

`vms-dpp-reference-probe` is a deliberately disposable qualification tool for
an unpersonalized reference image. It answers only two questions:

1. Can this exact Pi radio, kernel, driver, firmware, and `wpa_supplicant`
   complete DPP enrollee operation?
2. Can the selected Android phone act as the configurator for that device?

It is not the product onboarding implementation. The probe refuses to run when
`/etc/vms/device.json` exists, temporarily stops NetworkManager, generates an
ephemeral bootstrap identity, prints the public DPP URI to the UART/system
console, and listens for five minutes. Received network configuration exists
only in a root-only directory under `/run`; cleanup terminates the supplicant,
deletes that state, and restores NetworkManager. A successful authenticated
Wi-Fi association proves the radio/phone exchange, not route handoff, identity
claim, persistence, reset, recovery, or sellable onboarding.

For the current US pilot, install the script and unit into the unpersonalized
reference card, write `US` plus a newline to
`/boot/firmware/vms-dpp-reference-probe`, enable the service, and boot with UART
attached. Convert the URI between the begin/end markers to a QR code on the
development computer. Use Android's system Easy Connect surface to scan it.
Never paste a home Wi-Fi passphrase into the probe, shell, URL, or OSE portal.

Remove the unit, script, marker, and any probe journal before turning the card
into a camera. The permanent implementation must use a persistent unique
bootstrap key tied to the physical label, keep OSE claim authority separate,
and pass the governing secure-onboarding and transactional-route tests.
