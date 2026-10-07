"""KontainKeeper 管理服务端."""

__version__ = "0.1.0"
# 协议版本必须与 Agent 侧 kk_agent/config.PROTO_VER 同步。
# v3 = 去 token、上行帧带 ip（白名单校验）；
# v4 = 上报对象扩为任意 Linux 主机：status 帧新增 env/group/labels/caps。
PROTO_VER = 4
# v4 的兼容窗口（P1）：协议里 v3 → v4 只做「新增可选字段」，服务端没有必要因为一次
# 版本号上涨就把存量 Agent 全部判为不匹配（那等于一次升级全网闪断）。窗口期内 v3
# 帧照常落库，总览页用 proto_ver 列把「待升级」标出来。
# 关闭窗口：KK_DROP_PROTO_V3=1（`_accept_proto_vers()`），AGENTS.md 协议四件套同步。
ACCEPT_PROTO_VERS = (3, 4)
