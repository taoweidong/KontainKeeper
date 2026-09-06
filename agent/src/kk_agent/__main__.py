"""入口：python -m kk_agent（或编译后的 ./kk-agent 二进制，由 entrypoint-wrapper 后台拉起）。

最简运行形态（v3 起 Broker 匿名、无 token）：

    ./kk-agent mqtt://broker:1883            # 位置参数 = 服务端地址
    KK_SERVER=mqtt://broker:1883 ./kk-agent  # 等价的环境变量写法

PyInstaller 将本文件作为顶层 __main__ 直接执行（无父包上下文），相对导入
`from .main import run` 会抛 ImportError；故优先尝试绝对导入 kk_agent.main，
失败（源码包上下文）再回退到相对导入，两种运行方式都可用。
"""
import sys


def main():
    try:
        from kk_agent.main import run
    except ImportError:
        from .main import run
    overrides = {}
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        # 位置参数优先于 KK_SERVER 环境变量：二进制拉起只需一个地址
        overrides["server"] = sys.argv[1].strip()
    run(overrides=overrides)


if __name__ == "__main__":
    main()
