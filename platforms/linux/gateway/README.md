# Owner Gateway

This is the browser-only remote-access option. The customer owns the droplet,
domain, account, and bill; the product operator owns no shared gateway and
receives no camera traffic or credentials.

The unavoidable owner steps are:

1. Create a current Debian or Ubuntu droplet and point a hostname at it.
2. Open the gateway release bundle and run `sudo ./setup`; enter that hostname
   when asked. For unattended/repeat setup, the hostname may instead be the
   only line in `/root/vms-domain`.
3. On the recorder, run `sudo vms remote setup`, choose **2**, and enter the two
   values printed by the installer.

The installer generates the one-time enrollment code. Enrollment happens over
the hostname's valid HTTPS certificate. The recorder generates its own SSH
key, receives the gateway host key through that pinned HTTPS connection, and
then establishes a persistent outbound tunnel. No key is copied by hand, no
home-router port is opened, and camera ports are never published.

The installer enables the host firewall with only inbound TCP 22, 80, and 443.
Its enrollment and tunnel backends listen on loopback only. A matching
DigitalOcean Cloud Firewall is optional defense in depth, not another required
customer setup step.
