#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# xieyin 载荷：停止并移除 systemd 单元（需要 root）。幂等。
set -u
U=/etc/systemd/system

systemctl stop xieyin.service    >/dev/null 2>&1
systemctl disable xieyin.service >/dev/null 2>&1
rm -f "$U/xieyin.service"
systemctl daemon-reload
systemctl reset-failed >/dev/null 2>&1
exit 0
