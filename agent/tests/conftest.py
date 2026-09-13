"""Agent 测试公共固件：把 src 注入路径。

历史上这里还有个 make_fake_fs 伪造 /proc 树的工具，采集器改用 psutil 后
它已无从注入（psutil 读真实内核），故删除；采集测试改为一律 mock psutil。
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _reset_update_backoff():
    """updater 的失败退避是**模块级**状态（B6.3）。

    不清就会跨用例污染：前一个用例让 9.9.9 失败进入退避，后一个用例再用 9.9.9
    就会被直接跳过（拿到 backoff 而非预期原因）。每个用例前后各清一次。
    """
    from kk_agent import updater

    updater.reset_update_failure()
    yield
    updater.reset_update_failure()
