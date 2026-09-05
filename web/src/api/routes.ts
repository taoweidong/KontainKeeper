import { http } from "@/utils/http";

type Result = {
  success: boolean;
  data: Array<any>;
};

/** 动态路由改为静态声明：所有业务路由都来自 `src/router/modules/*.ts`，
 *  接口返回空数组让菜单直接从 constantMenus 拼装，dev/prod 行为一致。
 * 真正的异步路由场景（按角色下发菜单）将来若恢复，再切回 http.request。 */
export const getAsyncRoutes = (): Promise<Result> => {
  return Promise.resolve({ success: true, data: [] });
};

// 保留 http 引用防止纯静态后 ESLint unused-import 误报（vite 构建也会报 unused）
void http;