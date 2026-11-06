#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# xieyin 载荷：写出 systemd 单元并 reload（需要 root）。幂等。
set -u
DIR=/usr/local/xieyin
U=/etc/systemd/system

cat > "$U/xieyin.service" <<EOF
[Unit]
Description=xieyin - media metadata scraper for fnOS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=XIEYIN_DATA=$DIR/data
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $DIR/app_server.py serve
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
exit 0
