"""System service definition installed on a camera node."""

CAMERA_SERVICE = """[Unit]
Description=Video Monitoring Camera
After=local-fs.target
ConditionPathExists=/etc/vms/device.json

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/vms/camera_agent.py
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=vms-camera
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/etc/vms

[Install]
WantedBy=multi-user.target
"""


NETWORK_RECOVERY_SERVICE = """[Unit]
Description=Video Monitoring Camera Network Recovery
After=NetworkManager.service
Wants=NetworkManager.service
ConditionPathExists=/etc/vms/device.json

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/vms/network_recovery.py
TimeoutStartSec=90
StandardOutput=journal
StandardError=journal
SyslogIdentifier=vms-network-recovery
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/vms

[Install]
WantedBy=multi-user.target
"""


NETWORK_RECOVERY_TIMER = """[Unit]
Description=Check Video Monitoring Camera Network

[Timer]
OnBootSec=30s
OnUnitInactiveSec=30s
AccuracySec=5s
Unit=vms-network-recovery.service

[Install]
WantedBy=timers.target
"""
