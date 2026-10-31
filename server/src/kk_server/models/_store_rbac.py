"""Store 的 RBAC 域方法：用户（kk_admins 复用）/ 角色 / 菜单 / 角色菜单授权。

范围裁决（2026-10-31）：**权限只控菜单**——按钮级权限、数据级越权过滤都不做。
菜单树与前端静态路由的 name 一一对应（页面集合固定），菜单管理只编辑展示
元数据（标题/图标/排序/是否显示）与全局启停，不支持新建路由型菜单：新建的
菜单指向不存在的页面只会得到 404。

授权口径（allowed_menus）：
- admin 角色（built_in）：直通全部启用菜单，不落 role_menus 行——防止管理员
  把自己的菜单改没后无从恢复（恢复手段只剩改库）。
- 其他角色：role_menus 行 ∩ enabled=1 的菜单；角色行存在而授权行为空 = 有意
  清空（一个菜单都不给），不是「未初始化」。
"""

import secrets
import time

from sqlalchemy import and_, delete, insert, select, update

from .helpers import _pwdf
from .tables import admins, menus, role_menus, roles, sessions


# ---- 内置种子（name 与 web/src/router/modules/*.ts 的路由 name 逐字对应）----
# 组件视图路径：glob key 逐字对应 /src/views/**；分组固定 "Layout"（前端特判）。
_V = "/src/views"
_BUILTIN_MENUS = [
    # (name, parent, title, icon, rank, path, component, show_link)
    # Home 组的 path 刻意给 /home 而不是 /：前端注册了一个隐藏的 Root 容器
    # （path /，remaining 语义之外的全部动态路由挂在它下面记账），组自身
    # 从不被导航（单子项折叠为「首页」一个菜单项），路径唯一即可。
    ("Home", "", "首页", "ep/home-filled", 0, "/home", "Layout", 1),
    ("Welcome", "Home", "首页", "", 1, "/welcome", f"{_V}/welcome/index.vue", 1),
    ("Hosts", "", "主机管理", "ep/monitor", 1, "/hosts", "Layout", 1),
    ("HostMonitor", "Hosts", "主机总览", "", 1, "/hosts/monitor", f"{_V}/host/monitor/index.vue", 1),
    ("HostDetail", "Hosts", "主机详情", "", 2, "/hosts/detail/:pod", f"{_V}/host/detail/index.vue", 0),
    ("HostTrend", "Hosts", "使用趋势", "", 3, "/hosts/trend/:pod", f"{_V}/host/trend/index.vue", 0),
    ("HostUpdate", "Hosts", "版本与更新", "", 4, "/hosts/update", f"{_V}/host/update/index.vue", 1),
    ("CommandCenter", "", "命令中心", "ep/operation", 2, "/command", "Layout", 1),
    ("CommandShell", "CommandCenter", "命令面板", "", 1, "/command/shell", f"{_V}/command/shell/index.vue", 1),
    ("CommandCollect", "CommandCenter", "采集面板", "", 2, "/command/collect", f"{_V}/command/collect/index.vue", 1),
    ("Audit", "", "审计日志", "ep/document", 3, "/audit", "Layout", 1),
    ("AuditIndex", "Audit", "审计日志", "", 1, "/audit/index", f"{_V}/audit/index.vue", 1),
    ("System", "", "系统统计", "ep/odometer", 4, "/system", "Layout", 1),
    ("SystemStats", "System", "系统统计", "", 1, "/system/index", f"{_V}/system/index.vue", 1),
    ("Rbac", "", "系统管理", "ep/setting", 5, "/rbac", "Layout", 1),
    ("SysUser", "Rbac", "用户管理", "", 1, "/rbac/users", f"{_V}/system/user/index.vue", 1),
    ("SysRole", "Rbac", "角色管理", "", 2, "/rbac/roles", f"{_V}/system/role/index.vue", 1),
    ("SysMenu", "Rbac", "菜单管理", "", 3, "/rbac/menus", f"{_V}/system/menu/index.vue", 1),
]

_BUILTIN_ROLES = [
    # (name, label, remark, built_in)
    ("admin", "管理员", "内置：全部菜单（固定，不可改授权）", 1),
    ("operator", "运维", "内置：业务菜单（无系统管理）", 1),
]

# operator 的默认授权 = 内置菜单去掉「系统管理」子树
_OPERATOR_MENUS = [m[0] for m in _BUILTIN_MENUS
                   if m[0] != "Rbac" and m[1] != "Rbac"]


class _StoreRbacMixin:

    @staticmethod
    def _menu_json(row):
        """菜单行的 API 形态：整型标志位转布尔（show_link/enabled/built_in）。

        必须在出口处转：前端 filterTree 按 `meta.showLink !== false` 判显隐，
        JSON 里的 0 !== false 会让「不出侧边栏」的菜单漏进菜单渲染，进而在
        RouterLink 解析 /hosts/detail/:pod 这类带参路由时直接崩（2026-10-31
        浏览器实测）。布尔契约在这里收口，页面与过滤层不再各写一份转换。
        """
        d = dict(row)
        for k in ("show_link", "enabled", "built_in"):
            d[k] = bool(d[k])
        return d

    # ---- 种子（幂等：角色/菜单行缺则补；授权只对「尚无授权行的新角色」落默认值，
    #      管理员改过的授权不会被启动重置）----
    async def seed_rbac(self):
        now = int(time.time())
        for name, parent, title, icon, rank, path, comp, show in _BUILTIN_MENUS:
            row = await self._one(select(menus).where(menus.c.name == name))
            if row is None:
                await self._run(insert(menus).values(
                    name=name, parent=parent, title=title, icon=icon,
                    rank=rank, path=path, component=comp,
                    show_link=show, enabled=1, built_in=1))
            elif not row["path"] or not row["component"]:
                # 一阶段升级库（只有展示元数据列）：回填路由注册数据。
                # 只回填 built_in 行——内置行的 path/component 从不可编辑，
                # 用户改过的标题/图标/排序/显隐都保留。
                await self._run(update(menus).where(menus.c.name == name)
                                .values(path=path, component=comp))
        for name, label, remark, built_in in _BUILTIN_ROLES:
            row = await self._one(select(roles.c.name).where(roles.c.name == name))
            if row is None:
                await self._run(insert(roles).values(
                    name=name, label=label, remark=remark,
                    built_in=built_in, created=now))
                if name == "operator":
                    for m in _OPERATOR_MENUS:
                        await self._run(insert(role_menus).values(
                            role=name, menu=m))
        # 既有库升级兜底：补列默认 admin 已覆盖存量行，这里只清可能的空串
        await self._run(update(admins).where(admins.c.role == "")
                        .values(role="admin"))

    # ---- 菜单 ----
    async def all_menus(self):
        rows = await self._all(select(menus).order_by(menus.c.rank, menus.c.name))
        return [self._menu_json(r) for r in rows]

    async def get_menu(self, name):
        row = await self._one(select(menus).where(menus.c.name == name))
        return dict(row) if row else None

    async def update_menu(self, name, **meta):
        fields = {k: v for k, v in meta.items()
                  if k in ("title", "icon", "rank", "show_link", "enabled")
                  and v is not None}
        if not fields:
            return False
        await self._run(update(menus).where(menus.c.name == name).values(**fields))
        return True

    async def allowed_menus(self, role):
        """角色可见的启用菜单（[{name,title,icon,rank,show_link}]，rank 升序）。"""
        if role == "admin":
            cond = menus.c.enabled == 1
        else:
            granted = await self._all(select(role_menus.c.menu)
                                      .where(role_menus.c.role == role))
            names = [g["menu"] for g in granted]
            if not names:
                return []
            cond = and_(menus.c.enabled == 1, menus.c.name.in_(names))
        rows = await self._all(select(menus).where(cond)
                               .order_by(menus.c.rank, menus.c.name))
        return [self._menu_json(r) for r in rows]

    # ---- 角色 ----
    async def list_roles(self):
        rows = await self._all(select(roles).order_by(roles.c.created, roles.c.name))
        granted_rows = await self._all(select(role_menus))
        per_role = {}
        for g in granted_rows:
            per_role.setdefault(g["role"], []).append(g["menu"])
        user_rows = await self._all(select(admins.c.role))
        per_user = {}
        for u in user_rows:
            per_user[u["role"]] = per_user.get(u["role"], 0) + 1
        return [{"name": r["name"], "label": r["label"], "remark": r["remark"],
                 "built_in": bool(r["built_in"]), "created": r["created"],
                 "menus": sorted(per_role.get(r["name"], [])),
                 "users": per_user.get(r["name"], 0)}
                for r in rows]

    async def get_role(self, name):
        row = await self._one(select(roles).where(roles.c.name == name))
        return dict(row) if row else None

    async def create_role(self, name, label, remark=""):
        await self._run(insert(roles).values(
            name=name, label=label, remark=remark, built_in=0,
            created=int(time.time())))

    async def update_role(self, name, label=None, remark=None, menu_list=None):
        fields = {}
        if label is not None:
            fields["label"] = label
        if remark is not None:
            fields["remark"] = remark
        if fields:
            await self._run(update(roles).where(roles.c.name == name)
                            .values(**fields))
        if menu_list is not None:
            await self._run(delete(role_menus).where(role_menus.c.role == name))
            for m in menu_list:
                await self._run(insert(role_menus).values(role=name, menu=m))

    async def delete_role(self, name):
        await self._run(delete(roles).where(roles.c.name == name))
        await self._run(delete(role_menus).where(role_menus.c.role == name))

    # ---- 用户（kk_admins 复用，RBAC 加 role/enabled 两列）----
    async def get_user(self, username):
        row = await self._one(select(admins).where(admins.c.username == username))
        return dict(row) if row else None

    async def get_user_role(self, username):
        row = await self._one(select(admins.c.role)
                              .where(admins.c.username == username))
        return row["role"] if row else None

    async def list_users(self):
        rows = await self._all(select(admins).order_by(admins.c.created,
                                                       admins.c.username))
        role_labels = {r["name"]: r["label"]
                       for r in await self._all(select(roles))}
        return [{"username": r["username"], "role": r["role"],
                 "role_label": role_labels.get(r["role"], r["role"]),
                 "enabled": bool(r["enabled"]), "created": r["created"]}
                for r in rows]

    async def create_user(self, username, password, role):
        salt = secrets.token_hex(16)
        await self._run(insert(admins).values(
            username=username, salt=salt, pw_hash=_pwdf(salt, password),
            created=int(time.time()), role=role, enabled=1))

    async def update_user(self, username, role=None, password=None,
                          enabled=None):
        fields = {}
        if role is not None:
            fields["role"] = role
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        if fields:
            await self._run(update(admins).where(admins.c.username == username)
                            .values(**fields))
        if password:
            salt = secrets.token_hex(16)
            await self._run(update(admins).where(admins.c.username == username)
                            .values(salt=salt, pw_hash=_pwdf(salt, password)))
        if enabled is False:
            # 禁用即吊销会话：12h TTL 的旧 token 不能继续用
            await self._run(delete(sessions).where(sessions.c.username == username))

    async def delete_user(self, username):
        await self._run(delete(admins).where(admins.c.username == username))
        await self._run(delete(sessions).where(sessions.c.username == username))

    async def count_enabled_admins(self):
        row = await self._one(
            select(admins.c.username).where(
                (admins.c.role == "admin") & (admins.c.enabled == 1)))
        return 1 if row else 0
