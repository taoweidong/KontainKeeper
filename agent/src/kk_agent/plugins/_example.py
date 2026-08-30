# 示例自定义采集插件（下划线开头的文件不会被加载，改名 .py 去掉下划线即生效）
#
# 契约：
#   - 实现 collect() -> dict，返回值必须 JSON 可序列化
#   - 每次心跳前调用；文件 mtime 变化即热加载
#   - 抛异常只会跳过本次该插件的数据，不影响 Agent


def collect():
    return {
        "python": "ok",
        "pid_note": "put your custom probe here",
    }
