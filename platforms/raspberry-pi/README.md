# Raspberry Pi Platform

This directory packages the generic Video Monitoring System roles for
Raspberry Pi hardware. Product behavior, identity, recording, and setup remain
in `src/video_monitoring_system`; this directory contains only Pi image inputs
and hardware integration.

`camera-image/pilot` is the shared Zero W and Zero 2 W development image.
`recorder-image/pilot` builds the generic preinstalled Pi 4 recorder image.
`recorder-pilot` remains the manual recovery path for development only.

No customer, site, credential, or device identity belongs here.
