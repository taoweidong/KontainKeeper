"""Agent 测试公共固件：把 src 注入路径。

历史上这里还有个 make_fake_fs 伪造 /proc 树的工具，采集器改用 psutil 后
它已无从注入（psutil 读真实内核），故删除；采集测试改为一律 mock psutil。
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
