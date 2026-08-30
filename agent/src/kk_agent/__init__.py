"""KontainKeeper 容器端 Agent（纯标准库，可编译为单文件二进制）。

运行方式：
- 源码：  python -m kk_agent
- 二进制：./kk-agent   （由 agent/build 编译，运行时无需 Python）
"""
from .config import AGENT_VER, PROTO_VER

__version__ = "0.1.0"

__all__ = ["AGENT_VER", "PROTO_VER", "__version__"]
