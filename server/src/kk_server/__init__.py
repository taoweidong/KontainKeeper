"""KontainKeeper 管理服务端。"""

__version__ = "0.1.0"
# 协议版本必须与 Agent 侧 kk_agent/config.PROTO_VER 同步；v3 = 去 token、上行帧带 ip（白名单校验）。
PROTO_VER = 3
