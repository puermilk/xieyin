#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# xieyin 载荷：停服务、删单元，保留 /usr/local/xieyin/data。
set -u
DIR=/usr/local/xieyin

bash "$DIR/teardown_units.sh" >/dev/null 2>&1
# 彻底清干净：rm -rf /usr/local/xieyin
exit 0
