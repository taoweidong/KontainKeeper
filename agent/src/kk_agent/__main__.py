"""入口：python -m kk_agent（或编译后的 ./kk-agent 二进制，由 entrypoint-wrapper 后台拉起）。

PyInstaller 将本文件作为顶层 __main__ 直接执行（无父包上下文），相对导入
`from .main import run` 会抛 ImportError；故优先尝试绝对导入 kk_agent.main，
失败（源码包上下文）再回退到相对导入，两种运行方式都可用。
"""


def main():
    try:
        from kk_agent.main import run
    except ImportError:
        from .main import run
    run()


if __name__ == "__main__":
    main()
