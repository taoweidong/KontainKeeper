/** RBAC API 层契约锁：URL/method 逐条对齐后端 controllers/rbac.py（QR-W20）。
 *  路径参数必须编码：用户名/角色名/菜单名拼进 URL path，含特殊字符会打歪请求。 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/utils/http", () => ({ http: { request: vi.fn() } }));

import { http } from "@/utils/http";
import {
  createRole,
  createUser,
  deleteRole,
  deleteUser,
  listMenus,
  listRoles,
  listUsers,
  updateMenu,
  updateRole,
  updateUser
} from "@/api/rbac";

const req = vi.mocked(http.request);

beforeEach(() => {
  req.mockReset();
  req.mockResolvedValue({} as never);
});

describe("RBAC API：URL/method 契约", () => {
  it("用户管理三条路径", async () => {
    await listUsers();
    expect(req).toHaveBeenCalledWith("get", "/api/system/users");
    await createUser({ username: "u1", password: "p", role: "operator" });
    expect(req).toHaveBeenCalledWith("post", "/api/system/users", {
      data: { username: "u1", password: "p", role: "operator" }
    });
    await updateUser("u1", { role: "admin" });
    expect(req).toHaveBeenCalledWith(
      "put",
      "/api/system/users/u1",
      { data: { role: "admin" } }
    );
    await deleteUser("u1");
    expect(req).toHaveBeenCalledWith("delete", "/api/system/users/u1");
  });

  it("路径参数含特殊字符时必须编码（QR-W20）", async () => {
    await updateUser("a b/c", { enabled: false });
    expect(req).toHaveBeenCalledWith(
      "put",
      "/api/system/users/a%20b%2Fc",
      { data: { enabled: false } }
    );
    await deleteRole("空角色");
    expect(req).toHaveBeenCalledWith(
      "delete",
      `/api/system/roles/${encodeURIComponent("空角色")}`
    );
  });

  it("角色与菜单管理路径", async () => {
    await listRoles();
    expect(req).toHaveBeenCalledWith("get", "/api/system/roles");
    await createRole({ name: "r1", label: "R", remark: "" });
    expect(req).toHaveBeenCalledWith("post", "/api/system/roles", {
      data: { name: "r1", label: "R", remark: "" }
    });
    await updateRole("r1", { menus: ["Hosts"] });
    expect(req).toHaveBeenCalledWith(
      "put",
      "/api/system/roles/r1",
      { data: { menus: ["Hosts"] } }
    );
    await listMenus();
    expect(req).toHaveBeenCalledWith("get", "/api/system/menus");
    await updateMenu("Hosts", { title: "主机" });
    expect(req).toHaveBeenCalledWith(
      "put",
      "/api/system/menus/Hosts",
      { data: { title: "主机" } }
    );
  });
});
