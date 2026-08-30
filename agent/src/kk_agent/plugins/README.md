# 自定义采集插件

任意 `*.py`（下划线开头除外）放入本目录即自动生效，随心跳 `custom` 字段上报：

```python
def collect() -> dict:
    return {"queue_len": 3}   # 必须返回 JSON 可序列化对象
```

- 文件 mtime 变化即热重载，无需重启 Agent 与容器
- 单个插件失败只影响自身，异常会被记录到 agent 日志
- 管理端可通过命令控制台下发 `kind=plugin_reload` 立即触发采集并回传摘要
