"""KontainKeeper 主机端 Agent（MQTT 上报 + 远程命令）。

运行方式：
- 源码：  python -m kk_agent
- 二进制：./kk-agent   （由 agent/build（PyInstaller）编译，运行时无需 Python）

协议：经由 MQTT Broker 上报心跳/状态/命令结果，并接收服务端下发的命令。
连接可靠性（重连退避、遗嘱离线、离线命令排队）由 paho-mqtt 提供，
指标采集由 psutil 提供。
"""
from .config import AGENT_VER, PROTO_VER

__version__ = "0.2.0"

__all__ = ["AGENT_VER", "PROTO_VER", "__version__"]
