"""版本号比较工具（服务端侧；Agent 端有功能等价的纯标准库副本）。"""


def parse_version(v):
    out = []
    for p in str(v or "").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def version_lt(a, b):
    pa, pb = parse_version(a), parse_version(b)
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    return pa < pb


def count_outdated(version_counts, latest):
    """落后主机数（D1.3）：「某版本的主机各自有多少台」→ 落后总数。

    入参是 `{版本: 台数}` 直方图而不是逐台版本列表：500 台时逐台取回再比的代价与
    列表接口同级，而不同版本通常只有个位数。**判定必须在服务端做** —— 前端没有
    等价实现，让它自己比就是两套语义各自演化（`1.10.0` vs `1.9.0` 最先出错）。

    未上传过任何版本（`latest` 为空）时返回 0：此时「落后」无从定义，不是「全都落后」。
    """
    if not latest:
        return 0
    return sum(n for ver, n in (version_counts or {}).items()
               if version_lt(ver or "", latest))
