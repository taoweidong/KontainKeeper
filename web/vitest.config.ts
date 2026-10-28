import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // 默认 node 环境：业务页共用的纯逻辑层（utils/kk.ts、utils/kkPoll.ts）不碰 DOM。
    // 需要浏览器 API 的用例在文件头用 `// @vitest-environment jsdom` 单独声明。
    environment: "node",
    include: ["src/**/*.spec.ts"],
    // 与 pnpm build 同尺：CI 的红灯必须能复现本地红灯
    reporters: "default"
  }
});
