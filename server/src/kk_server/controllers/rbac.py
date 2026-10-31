"""RBAC 管理接口：用户 / 角色 / 菜单（权限范围=菜单控制，见 models/_store_rbac.py）。

- 管理类接口统一挂 `AdminUser` 依赖（登录 + admin 角色）；
- `GET /api/auth/menus` 只要求登录：返回当前用户的角色与可见菜单，
  前端在 initRouter 时拉取并据此过滤侧边栏与路由守卫（菜单级 RBAC）。
- 所有变更落审计（store.add_audit）：RBAC 变更是安全事件，必须可追溯。
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .deps import AdminUser, CurrentUser

router = APIRouter(prefix="/api")

_USERNAME_MIN = 2


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "operator"


class UserUpdate(BaseModel):
    role: str | None = None
    password: str | None = None
    enabled: bool | None = None


class RoleCreate(BaseModel):
    name: str
    label: str
    remark: str = ""


class RoleUpdate(BaseModel):
    label: str | None = None
    remark: str | None = None
    menus: list[str] | None = None


class MenuUpdate(BaseModel):
    title: str | None = None
    icon: str | None = None
    rank: int | None = None
    show_link: bool | None = None
    enabled: bool | None = None


def _clean(s: str, field: str, lo: int, hi: int) -> str:
    s = (s or "").strip()
    if not (lo <= len(s) <= hi):
        raise HTTPException(status_code=422, detail=f"{field} 长度须在 {lo}-{hi} 之间")
    return s


# ---- 当前用户：角色 + 可见菜单（前端 initRouter 拉取）----

@router.get("/auth/menus")
async def auth_menus(user: CurrentUser, request: Request):
    """当前用户的角色与可见菜单（不含禁用项）。

    注意返回的是**允许集合**而非全量菜单树：前端 fail-closed——集合之外的一律
    不渲染、路由守卫一律 403。接口挂了前端拿到空集合，宁可不显示也不能错显示。
    """
    store = request.app.state.store
    role = await store.get_user_role(user) or "admin"
    return {"roles": [role], "menus": await store.allowed_menus(role)}


# ---- 用户管理 ----

@router.get("/system/users")
async def users_list(_: AdminUser, request: Request):
    return {"items": await request.app.state.store.list_users()}


@router.post("/system/users")
async def users_create(body: UserCreate, admin: AdminUser,
                       request: Request):
    store = request.app.state.store
    username = _clean(body.username, "用户名", _USERNAME_MIN, 64)
    if len(body.password) < 6:
        raise HTTPException(status_code=422, detail="密码至少 6 位")
    if await store.get_user(username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    if not await store.get_role(body.role):
        raise HTTPException(status_code=422, detail="角色不存在")
    await store.create_user(username, body.password, body.role)
    await store.add_audit(admin, "user_create",
                          {"user": username, "role": body.role})
    return {"ok": True}


@router.put("/system/users/{username}")
async def users_update(username: str, body: UserUpdate, admin: AdminUser,
                       request: Request):
    store = request.app.state.store
    if not await store.get_user(username):
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.role and not await store.get_role(body.role):
        raise HTTPException(status_code=422, detail="角色不存在")
    if body.password is not None and len(body.password) < 6:
        raise HTTPException(status_code=422, detail="密码至少 6 位")
    # 最后一个启用的管理员不能被禁用/降级：RBAC 管理入口不能被自己锁死
    if (username == admin and await store.count_enabled_admins() <= 1
            and ((body.enabled is False) or (body.role not in (None, "admin")))):
        raise HTTPException(status_code=409, detail="不能停用/降级最后一个管理员")
    await store.update_user(username, role=body.role, password=body.password,
                            enabled=body.enabled)
    await store.add_audit(admin, "user_update",
                          {"user": username,
                           "role": body.role, "enabled": body.enabled,
                           "reset_pwd": body.password is not None})
    return {"ok": True}


@router.delete("/system/users/{username}")
async def users_delete(username: str, admin: AdminUser,
                       request: Request):
    store = request.app.state.store
    if username == admin:
        raise HTTPException(status_code=409, detail="不能删除当前登录账号")
    if not await store.get_user(username):
        raise HTTPException(status_code=404, detail="用户不存在")
    if (await store.get_user_role(username) == "admin"
            and await store.count_enabled_admins() <= 1):
        raise HTTPException(status_code=409, detail="不能删除最后一个管理员")
    await store.delete_user(username)
    await store.add_audit(admin, "user_delete", {"user": username})
    return {"ok": True}


# ---- 角色管理 ----

@router.get("/system/roles")
async def roles_list(_: AdminUser, request: Request):
    return {"items": await request.app.state.store.list_roles()}


@router.post("/system/roles")
async def roles_create(body: RoleCreate, admin: AdminUser,
                       request: Request):
    store = request.app.state.store
    name = _clean(body.name, "角色标识", 2, 32)
    if not name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=422, detail="角色标识仅限字母/数字/-/_")
    if await store.get_role(name):
        raise HTTPException(status_code=409, detail="角色已存在")
    await store.create_role(name, _clean(body.label, "角色名", 1, 64),
                            body.remark or "")
    await store.add_audit(admin, "role_create", {"role": name})
    return {"ok": True}


@router.put("/system/roles/{name}")
async def roles_update(name: str, body: RoleUpdate, admin: AdminUser,
                       request: Request):
    store = request.app.state.store
    role = await store.get_role(name)
    if not role:
        raise HTTPException(status_code=404, detail="角色不存在")
    if body.menus is not None:
        if role["built_in"] and name == "admin":
            raise HTTPException(status_code=409,
                                detail="admin 角色的授权固定为全量，不可修改")
        known = {m["name"] for m in await store.all_menus()}
        unknown = [m for m in body.menus if m not in known]
        if unknown:
            raise HTTPException(status_code=422,
                                detail=f"未知菜单：{', '.join(unknown)}")
    await store.update_role(name, label=body.label, remark=body.remark,
                            menu_list=body.menus)
    await store.add_audit(admin, "role_update",
                          {"role": name, "menus": body.menus,
                           "label": body.label, "remark": body.remark})
    return {"ok": True}


@router.delete("/system/roles/{name}")
async def roles_delete(name: str, admin: AdminUser, request: Request):
    store = request.app.state.store
    role = await store.get_role(name)
    if not role:
        raise HTTPException(status_code=404, detail="角色不存在")
    if role["built_in"]:
        raise HTTPException(status_code=409, detail="内置角色不可删除")
    listing = await store.list_roles()
    in_use = next((r["users"] for r in listing if r["name"] == name), 0)
    if in_use:
        raise HTTPException(status_code=409,
                            detail=f"仍有 {in_use} 个用户使用该角色")
    await store.delete_role(name)
    await store.add_audit(admin, "role_delete", {"role": name})
    return {"ok": True}


# ---- 菜单管理 ----

@router.get("/system/menus")
async def menus_list(_: AdminUser, request: Request):
    return {"items": await request.app.state.store.all_menus()}


@router.put("/system/menus/{name}")
async def menus_update(name: str, body: MenuUpdate, admin: AdminUser,
                       request: Request):
    store = request.app.state.store
    if not await store.get_menu(name):
        raise HTTPException(status_code=404, detail="菜单不存在")
    if body.rank is not None and not (0 <= body.rank <= 999):
        raise HTTPException(status_code=422, detail="排序须在 0-999 之间")
    await store.update_menu(name, title=body.title, icon=body.icon,
                            rank=body.rank,
                            show_link=(None if body.show_link is None
                                       else int(body.show_link)),
                            enabled=(None if body.enabled is None
                                     else int(body.enabled)))
    await store.add_audit(admin, "menu_update",
                          {"menu": name, "title": body.title,
                           "enabled": body.enabled,
                           "show_link": body.show_link, "rank": body.rank})
    return {"ok": True}
