"""自定义采集插件包。

放置于 KK_PLUGIN_DIR（默认与本包同级的 plugins/）下的任意 *.py（下划线开头的文件
不会被加载），实现 collect() -> dict 即可随心跳 custom 字段上报。文件 mtime 变化即
热重载，无需重启 Agent。详见 plugins/README.md 与 _example.py。
"""
