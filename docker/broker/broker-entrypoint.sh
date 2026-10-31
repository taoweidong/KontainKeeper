#!/usr/bin/env sh
# kk-broker 入口：启动前把数据卷属主归位到 mosquitto 用户。
# 从 eclipse-mosquitto:2 升级来的旧 mosquitto-data 卷属主是 uid 1883，与
# ubuntu 的 mosquitto 用户 uid 不同；不归位的话 persistence 落盘会静默失败。
set -e
chown -R mosquitto:mosquitto /mosquitto/data 2>/dev/null || true
exec "$@"
