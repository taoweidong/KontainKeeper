import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import vueJsx from "@vitejs/plugin-vue-jsx";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  // 业务代码里的 JSX/TSX（如 `views/audit/utils/hook.tsx`）与 .vue 单测需要这两个插件，
  // 与 `build/plugins.ts` 的 `vue()` + `vueJsx()` 保持同一套解析规则。
  plugins: [vue(), vueJsx()],
  resolve: {
    // 与 vite.config.ts 的 `alias["@"]` 同尺：被测模块之间用 `@/` 互相引用，
    // 缺这条别名会在收集阶段就报 "Failed to load @/utils/http"。
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url))
    }
  },
  test: {
    // 默认 node 环境：业务页共用的纯逻辑层（utils/kk.ts、utils/kkPoll.ts）不碰 DOM。
    // 需要浏览器 API 的用例在文件头用 `// @vitest-environment jsdom` 单独声明。
    environment: "node",
    include: ["src/**/*.spec.ts", "src/**/*.spec.tsx"],
    // 覆盖率只统计业务逻辑层：脚手架（layout/components/router）不是本轮被测面，
    // 把它们算进分母只会稀释信号。
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      reportsDirectory: "coverage",
      include: [
        "src/api/**/*.ts",
        "src/utils/**/*.ts",
        "src/store/modules/**/*.ts",
        "src/views/**/utils/*.tsx"
      ],
      exclude: ["src/**/*.spec.ts", "src/**/*.spec.tsx", "src/utils/http/types.ts"]
    },
    // 与 pnpm build 同尺：CI 的红灯必须能复现本地红灯
    reporters: "default"
  }
});
