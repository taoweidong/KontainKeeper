# 前端独立镜像（默认不走该文件：前端产物随 kk-server 包内分发，见 docs/deployment.md §5）。
#
# 基础镜像对齐生产环境镜像清单：node-20 → node:20，nginx-1.20 → nginx:1.20；
# 内网 registry 命名不同时用 --build-arg NODE_BASE / NGINX_BASE / NPM_REGISTRY 覆盖。
ARG NODE_BASE=node:20
FROM ${NODE_BASE} AS build-stage

WORKDIR /app

# pnpm@9 与 CI（corepack prepare pnpm@9）及 pnpm-lock.yaml（lockfileVersion 9.0）对齐；
# 经 npm 源安装（不用 corepack：其 prepare 依赖 npm 签名密钥，历史上有轮换兼容坑）
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry "$NPM_REGISTRY" \
    && npm install -g pnpm@9

COPY .npmrc package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

ARG NGINX_BASE=nginx:1.20
FROM ${NGINX_BASE} AS production-stage

# SPA 路由 fallback（当前默认 hash 路由不依赖它，但 history 路由下刷新非根路径不再 404）
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build-stage /app/dist /usr/share/nginx/html
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
