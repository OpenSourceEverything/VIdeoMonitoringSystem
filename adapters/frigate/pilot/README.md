# Managed Frigate pilot adapter

This adapter runs pinned Frigate as a hidden OSE appliance component. Customers
do not invoke these files and never edit Frigate YAML.

`vms-frigate-render` asks the OSE application to derive private runtime files
from saved camera sources, stable engine bindings, and engine-neutral policy.
The generated YAML contains environment references, while authenticated camera
URLs remain in a mode-0600 environment file. Camera hostnames are resolved by
the recorder to a single non-public address before Docker receives them.

`vms-frigate-reconcile.path` reacts to saved product-state changes. The timer
also checks every two minutes so a DHCP address change self-corrects even when
no file changes. Reconciliation is serialized and restarts Frigate and the OSE
portal only when the application reports a real semantic change.

The same check also starts a stopped engine when configuration is unchanged.
Before writing, the Linux storage guard matches the paired volume identity,
rejects duplicate/system/foreign volumes, and checks real filesystem I/O.
A reconnected drive may change its device name: only a stale mount whose old
device is gone is detached. Recovery never formats a drive or runs automatic
filesystem repair. A damaged/read-only filesystem stops recording and requires
inspection; the portal remains available. Recovery is bounded and serialized.
For maintenance, stop the reconciliation timer/path before stopping the engine;
otherwise the next health check will restore its managed service.

`install-managed-adapter` is the guarded pilot migration. It requires the
pinned image to be preloaded, renders successfully before stopping a running
legacy supervisor, and restores that supervisor if the managed service cannot
start. The final appliance image must install this adapter during the normal
offline build/update process rather than asking an owner to run it.

Frigate's management API and go2rtc frame API are published on separate host
loopback ports 15000 and 15001. Neither API is exposed to the home LAN; VMS
uses the go2rtc frame endpoint only to verify a staged camera route.

Hardware encoding is opt-in after a sustained encode/stop/restart test, not
selected merely because a device node exists. Reinstallation preserves the
root-owned, mode-0440 `/etc/vms/frigate-hardware` selection. Legacy MJPEG
software conversion caps encoder threads and uses two-second keyframes;
`VMS_FRIGATE_MJPEG_SOFTWARE_PRESET` accepts `superfast` (default) or `ultrafast`.
The latter uses less encoding work but can increase bitrate and storage use.
Native H.264 inputs still bypass recorder encoding completely.
