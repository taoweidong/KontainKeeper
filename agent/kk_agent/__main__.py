"""入口：python -m kk_agent（或编译后的 ./kk-agent 二进制，由 entrypoint-wrapper 后台拉起）。"""
from .main import run


def main():
    run()


if __name__ == "__main__":
    main()
