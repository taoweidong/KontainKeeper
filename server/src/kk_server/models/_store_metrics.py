"""Store 的 metrics 域方法（QR29-G 第二轮拆分）：经 Mixin 并入门面，调用点零改动。

方言收敛点（_upsert/_ensure_schema/_column_ddl）不在本文件——它们留在 store.py
基座（AGENTS.md 硬约束：三库方言差异全部收在基座两处，不许散回各域）。
本文件的改动必须是纯搬动：混进任何行为变更即视为违规（拆回）。
"""


class _StoreMetricsMixin:
    pass
