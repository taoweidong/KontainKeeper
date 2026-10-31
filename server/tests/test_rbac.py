"""RBAC（用户/角色/菜单，权限只控菜单）的接口与存储测试。

夹具绕过 lifespan（ASGI Transport 不触发启动事件），store.setup() 自带
seed_rbac 幂等种子——同时验证「二次 setup 不报错、不重置管理员改过的授权」。
"""

import httpx
import pytest
from sqlalchemy import select

from kk_server.main import create_app
from kk_server.models.tables import role_menus

ADMIN = "admin"
PASS = "rbac-pass-1"


@pytest.fixture
async def api(tmp_path):
    app = create_app({
        "KK_ADMIN_USER": ADMIN,
        "KK_ADMIN_PASS": PASS,
        "KK_DB_PATH": str(tmp_path / "rbac.db"),
        "KK_WEB_DIR": str(tmp_path / "noweb"),
    })
    store = app.state.store
    await store.setup()
    # 绕过 lifespan：ASGI Transport 不触发启动事件，这里手工补上等价初始化
    # （生产 lifespan 里 setup 之后的 ensure_admin 是登录可用的前提）
    await store.ensure_admin(ADMIN, PASS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield type("Ctx", (), {"client": client, "store": store, "app": app})
    await store.close()


async def _login(api, username=ADMIN, password=PASS):
    r = await api.client.post("/api/login", json={"username": username,
                                                  "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


async def _make_user(api, username, role="operator", password="ops-pass-1"):
    """管理员建用户并登录，返回该用户的鉴权头。"""
    admin_h = await _login(api)
    r = await api.client.post("/api/system/users", headers=admin_h,
                              json={"username": username, "password": password,
                                    "role": role})
    assert r.status_code == 200, r.text
    return await _login(api, username, password)


async def test_seed_and_auth_menus(api):
    """种子幂等 + 双角色菜单口径：admin 全量、operator 无系统管理子树。"""
    await api.store.setup()  # 二次 setup：种子不得报错
    h = await _login(api)
    r = await api.client.get("/api/auth/menus", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["roles"] == ["admin"]
    names = {m["name"] for m in body["menus"]}
    assert {"Welcome", "Home", "Hosts", "HostMonitor", "CommandShell",
            "AuditIndex", "SystemStats", "Rbac", "SysUser", "SysRole",
            "SysMenu"} <= names

    ops_h = await _make_user(api, "ops1")
    r = await api.client.get("/api/auth/menus", headers=ops_h)
    ops_names = {m["name"] for m in r.json()["menus"]}
    assert "HostMonitor" in ops_names and "HostUpdate" in ops_names
    assert not ({"Rbac", "SysUser", "SysRole", "SysMenu"} & ops_names)
    # 登录响应自带角色
    r = await api.client.post("/api/login", json={"username": "ops1",
                                                  "password": "ops-pass-1"})
    assert r.json()["roles"] == ["operator"]


async def test_disabled_user_login_and_session_revoked(api):
    """禁用：登录 403、会话即时吊销（旧 token 不能再用）。"""
    admin_h = await _login(api)
    h = await _make_user(api, "ops1")
    r = await api.client.get("/api/auth/menus", headers=h)
    assert r.status_code == 200

    r = await api.client.put("/api/system/users/ops1", headers=admin_h,
                             json={"enabled": False})
    assert r.status_code == 200
    r = await api.client.get("/api/auth/menus", headers=h)
    assert r.status_code == 401  # 会话已吊销
    r = await api.client.post("/api/login", json={"username": "ops1",
                                                  "password": "ops-pass-1"})
    assert r.status_code == 403
    assert "禁用" in r.json()["detail"]


async def test_admin_api_requires_admin_role(api):
    """运维角色访问 RBAC 管理接口一律 403；未登录 401。"""
    ops_h = await _make_user(api, "ops1")
    for method, path in (("get", "/api/system/users"),
                         ("post", "/api/system/users"),
                         ("get", "/api/system/roles"),
                         ("get", "/api/system/menus")):
        r = await getattr(api.client, method)(path, headers=ops_h)
        assert r.status_code == 403, (method, path, r.text)
    r = await api.client.get("/api/system/users")
    assert r.status_code == 401


async def test_user_crud_guards(api):
    """用户管理：重名 409、自删 409、最后一个管理员不可删/降级/禁用。"""
    admin_h = await _login(api)
    r = await api.client.post("/api/system/users", headers=admin_h,
                              json={"username": "ops1", "password": "ops-pass-1",
                                    "role": "operator"})
    assert r.status_code == 200
    r = await api.client.post("/api/system/users", headers=admin_h,
                              json={"username": "ops1", "password": "ops-pass-2",
                                    "role": "operator"})
    assert r.status_code == 409
    r = await api.client.post("/api/system/users", headers=admin_h,
                              json={"username": "x", "password": "short",
                                    "role": "operator"})
    assert r.status_code == 422
    r = await api.client.post("/api/system/users", headers=admin_h,
                              json={"username": "x", "password": "long-enough",
                                    "role": "no-such-role"})
    assert r.status_code == 422

    # 自删与最后一个管理员
    r = await api.client.delete("/api/system/users/admin", headers=admin_h)
    assert r.status_code == 409
    r = await api.client.put("/api/system/users/admin", headers=admin_h,
                             json={"role": "operator"})
    assert r.status_code == 409
    r = await api.client.put("/api/system/users/admin", headers=admin_h,
                             json={"enabled": False})
    assert r.status_code == 409

    # 重置密码：新口令可登录、旧口令失效
    r = await api.client.put("/api/system/users/ops1", headers=admin_h,
                             json={"password": "new-pass-9"})
    assert r.status_code == 200
    r = await api.client.post("/api/login", json={"username": "ops1",
                                                  "password": "new-pass-9"})
    assert r.status_code == 200
    r = await api.client.post("/api/login", json={"username": "ops1",
                                                  "password": "ops-pass-1"})
    assert r.status_code == 401

    # 删除普通用户（自删 409 已在上面覆盖）
    r = await api.client.delete("/api/system/users/ops1", headers=admin_h)
    assert r.status_code == 200

    items = (await api.client.get("/api/system/users",
                                  headers=admin_h)).json()["items"]
    assert "admin" in {u["username"] for u in items}
    assert all(u["role_label"] for u in items)


async def test_role_crud_and_menu_assignment(api):
    """角色管理：内置不可删、admin 授权不可改、自定义角色授权即时生效。"""
    admin_h = await _login(api)
    r = await api.client.delete("/api/system/roles/admin", headers=admin_h)
    assert r.status_code == 409
    r = await api.client.put("/api/system/roles/admin", headers=admin_h,
                             json={"menus": ["Welcome"]})
    assert r.status_code == 409

    r = await api.client.post("/api/system/roles", headers=admin_h,
                              json={"name": "viewer", "label": "只读",
                                    "remark": "只看主机"})
    assert r.status_code == 200
    r = await api.client.post("/api/system/roles", headers=admin_h,
                              json={"name": "viewer", "label": "重复"})
    assert r.status_code == 409

    r = await api.client.put("/api/system/roles/viewer", headers=admin_h,
                             json={"menus": ["Welcome", "Home", "Hosts",
                                             "HostMonitor"]})
    assert r.status_code == 200
    r = await api.client.put("/api/system/roles/viewer", headers=admin_h,
                             json={"menus": ["NoSuchMenu"]})
    assert r.status_code == 422

    await api.client.post("/api/system/users", headers=admin_h,
                          json={"username": "viewer1",
                                "password": "view-pass-1", "role": "viewer"})
    vh = await _login(api, "viewer1", "view-pass-1")
    menus = (await api.client.get("/api/auth/menus", headers=vh)).json()["menus"]
    assert {m["name"] for m in menus} == {"Welcome", "Home", "Hosts",
                                          "HostMonitor"}

    # 删除有用户的角色 → 409；摘掉用户后可删
    r = await api.client.delete("/api/system/roles/viewer", headers=admin_h)
    assert r.status_code == 409
    await api.client.delete("/api/system/users/viewer1", headers=admin_h)
    r = await api.client.delete("/api/system/roles/viewer", headers=admin_h)
    assert r.status_code == 200


async def test_menu_update_and_admin_role_bypass(api):
    """菜单元数据可编辑；admin 授权直通全量、不落 role_menus 行。"""
    admin_h = await _login(api)
    r = await api.client.put("/api/system/menus/HostMonitor", headers=admin_h,
                             json={"title": "主机看板", "rank": 9,
                                   "show_link": False})
    assert r.status_code == 200
    menu = await api.store.get_menu("HostMonitor")
    assert menu["title"] == "主机看板" and menu["rank"] == 9
    assert menu["show_link"] == 0
    # admin 的 allowed 不读 role_menus：直通全量（含刚改过的菜单）
    h = await _login(api)
    menus = (await api.client.get("/api/auth/menus", headers=h)).json()["menus"]
    assert "HostMonitor" in {m["name"] for m in menus}
    granted = await api.store._all(select(role_menus))
    assert all(g["role"] != "admin" for g in granted)
