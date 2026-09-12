#!/usr/bin/env groovy
// ============================================================================
// KontainKeeper Jenkins 流水线：测试 → 构建 → 镜像冒烟 → 推送 → 部署 → 验证
//
// 设计原则（与本仓库既有约定对齐）：
//   1. **复用仓库脚本，不在 CI 里重写构建逻辑**：
//      - Agent 二进制 → agent/build/build_binary.sh
//      - 服务端镜像  → server/Dockerfile（构建上下文必须是仓库根，uv workspace 锁在根）
//      - 部署        → docker-compose.prod.yml（与手工部署同一份文件，避免两套真相）
//      - Broker 冒烟 → scripts/mqtt_e2e.py（补单测证不到的 LWT / 离线排队语义）
//      - 镜像冒烟    → scripts/ci_smoke.sh（真起容器 + 真跑 Agent 二进制走完整链路）
//   2. **Broker 由流水线自己起**：server/tests 的 4 条集成用例在无 Broker 时自动 skip，
//      等于 CI 上是「198 passed + 4 skipped」的假绿。这里起一个 Mosquitto，把 4 条真正跑掉。
//   3. **凭据只走 Jenkins credentials**：仓库公开，.env 严禁入库（§部署阶段只经 stdin 落到目标机）。
//   4. **部署前后都有验证**：镜像冒烟保证「这个镜像能用」，部署验证保证「目标机上真的起来了」。
//
// 必需的 Jenkins 配置见 docs/ci-jenkins.md（节点要求、凭据 ID、触发方式、回滚步骤）。
// ============================================================================

pipeline {
    agent { label "${params.AGENT_LABEL}" }

    options {
        timestamps()
        // 部署类任务串行：并发跑两次会互相覆盖目标机的容器
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '40', artifactNumToKeepStr: '10'))
        timeout(time: 120, unit: 'MINUTES')
        skipDefaultCheckout(true)
        parallelsAlwaysFailFast()
    }

    parameters {
        string(name: 'AGENT_LABEL', defaultValue: 'docker',
               description: '构建节点标签：该节点需具备 docker + docker compose 插件 + python3 + git + curl')
        choice(name: 'ENVIRONMENT', choices: ['staging', 'production'],
               description: '部署环境：production 会额外推 latest 标签，并要求人工确认后才部署')

        string(name: 'IMAGE_REGISTRY', defaultValue: '',
               description: '镜像仓库前缀（如 harbor.ops.example.com/kk）。留空 = 只构建不推送，目标机改为本地构建')
        string(name: 'IMAGE_TAG', defaultValue: '',
               description: '镜像标签。留空 = sha-<短SHA>；**回滚时填上一个版本的标签**')

        string(name: 'DEPLOY_HOST', defaultValue: '', description: '部署目标机（SSH）。留空 = 不部署')
        string(name: 'DEPLOY_USER', defaultValue: 'deploy', description: '部署目标机 SSH 用户')
        string(name: 'DEPLOY_DIR', defaultValue: '/opt/kontainkeeper',
               description: '目标机上的仓库克隆路径（CI 会在其中 git reset --hard 到本次提交）')
        string(name: 'DEPLOY_HEALTH_URL', defaultValue: '',
               description: '部署后健康检查地址。留空 = http://<DEPLOY_HOST>:8443（走反代时填 https 地址）')
        string(name: 'ADMIN_USER', defaultValue: 'admin', description: '管理员用户名（部署验证登录用）')
        string(name: 'TOPIC_PREFIX', defaultValue: 'kk/v1', description: 'MQTT 主题前缀（须与 Agent 侧一致）')

        // CI Broker 用非默认端口，避免与节点上已有的开发 Broker 抢 1883
        string(name: 'CI_MQTT_PORT', defaultValue: '18830',
               description: 'CI 测试用 Broker 绑定端口（仅绑 127.0.0.1）')

        booleanParam(name: 'SKIP_TESTS', defaultValue: false, description: '跳过后端测试与 Broker 冒烟（仅应急）')
        booleanParam(name: 'SKIP_DEPLOY', defaultValue: false, description: '只构建不出部署')
        booleanParam(name: 'STRICT_WEB_SYNC', defaultValue: true,
               description: '前端产物与仓库已提交产物不一致时失败（推荐开着，防止忘记同步 web/dist 到包内）')
        booleanParam(name: 'AUTO_APPROVE', defaultValue: false,
               description: '生产部署免人工确认（默认需要点一次「确认部署」）')
        booleanParam(name: 'PACK_OFFLINE', defaultValue: false,
               description: '额外打包离线镜像 tar（内网无网部署用，会拉取全部基础镜像，较慢）')
    }

    environment {
        // ---- 由参数派生（declarative 的 environment 支持 params 插值）----
        ENVIRONMENT        = "${params.ENVIRONMENT}"
        REGISTRY           = "${params.IMAGE_REGISTRY}"
        DEPLOY_HOST        = "${params.DEPLOY_HOST}"
        DEPLOY_USER        = "${params.DEPLOY_USER}"
        DEPLOY_DIR         = "${params.DEPLOY_DIR}"
        ADMIN_USER         = "${params.ADMIN_USER}"
        TOPIC_PREFIX       = "${params.TOPIC_PREFIX}"

        IMAGE_NAME         = 'kontainkeeper-server'
        // 与 deploy/offline/pack.sh、docker-compose.offline.yml 约定的本地标签保持一致
        OFFLINE_IMAGE      = 'kk-server:latest'
        BROKER_IMAGE       = 'eclipse-mosquitto:2'
        BROKER_CT          = 'kk-ci-broker'
        CI_MQTT_PORT       = "${params.CI_MQTT_PORT}"
        CI_MQTT_URL        = "mqtt://127.0.0.1:${params.CI_MQTT_PORT}"

        // ---- 缓存落到 workspace，避免污染节点全局、又能在多次构建间复用 ----
        UV_CACHE_DIR       = "${WORKSPACE}/.ci-cache/uv"
        PIP_CACHE_DIR      = "${WORKSPACE}/.ci-cache/pip"
        CI_TOOLS           = "${WORKSPACE}/.ci-tools"
        AGENT_BUILD_VENV   = "${WORKSPACE}/.ci-tools/kkagent-build"
    }

    stages {

        // --------------------------------------------------------------------
        stage('① 检出与元信息') {
            steps {
                checkout scm
                script {
                    env.GIT_SHA   = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
                    env.GIT_SHORT = sh(returnStdout: true, script: 'git rev-parse --short=12 HEAD').trim()
                    def branch    = env.BRANCH_NAME?.trim()
                    env.GIT_REF   = branch ?: sh(returnStdout: true,
                                                 script: 'git rev-parse --abbrev-ref HEAD').trim()

                    env.IMAGE_TAG  = params.IMAGE_TAG?.trim() ?: "sha-${env.GIT_SHORT}"
                    env.LOCAL_IMAGE = "${env.IMAGE_NAME}:${env.IMAGE_TAG}"
                    env.FULL_IMAGE  = env.REGISTRY?.trim()
                                        ? "${env.REGISTRY}/${env.IMAGE_NAME}:${env.IMAGE_TAG}"
                                        : "${env.IMAGE_NAME}:${env.IMAGE_TAG}"
                    env.REGISTRY_HOST = env.REGISTRY?.trim() ? env.REGISTRY.split('/')[0] : ''
                    env.DEPLOY_HEALTH = params.DEPLOY_HEALTH_URL?.trim()
                                        ?: (env.DEPLOY_HOST?.trim() ? "http://${env.DEPLOY_HOST}:8443" : '')

                    echo """================ 本次构建 ================
提交      : ${env.GIT_SHA}  (${env.GIT_REF})
镜像      : ${env.FULL_IMAGE}
环境      : ${env.ENVIRONMENT}
部署目标  : ${env.DEPLOY_HOST ?: '（不部署）'}
CI Broker : ${env.CI_MQTT_URL}
=========================================="""
                }
            }
        }

        // --------------------------------------------------------------------
        stage('② 工具链自检') {
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail

missing=()
for t in docker python3 git curl node pnpm; do
  command -v "$t" >/dev/null 2>&1 || missing+=("$t")
done
docker compose version >/dev/null 2>&1 || missing+=("docker compose 插件")
if [ "${#missing[@]}" -gt 0 ]; then
  echo "!! 构建节点缺少：${missing[*]}"
  echo "   该节点需具备：docker + docker compose 插件 + git + curl + python3 + node(^20.19|>=22.13) + pnpm(>=9)"
  echo "   缺 pnpm 时：corepack enable && corepack prepare pnpm@9 --activate"
  echo "   详见 docs/ci-jenkins.md §2「节点要求」"
  exit 1
fi

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if [ "$node_major" -lt 20 ]; then
  echo "!! node 版本过低：$(node --version)（前端要求 ^20.19 || >=22.13，推荐 22，与 web/.nvmrc 一致）"
  exit 1
fi

# uv 缺失就装进 workspace（不写节点全局环境）：Astral 官方安装脚本
if ! command -v uv >/dev/null 2>&1; then
  echo ">> 节点无 uv，装入 $CI_TOOLS"
  mkdir -p "$CI_TOOLS"
  UV_INSTALL_DIR="$CI_TOOLS" UV_NO_MODIFY_PATH=1 \
    bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

echo ">> docker  : $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
echo ">> compose : $(docker compose version --short 2>/dev/null || echo unknown)"
echo ">> python3 : $(python3 --version 2>&1)"
echo ">> node    : $(node --version)  pnpm: $(pnpm --version)"
echo ">> uv      : $("$CI_TOOLS/uv" --version 2>/dev/null || uv --version 2>/dev/null || echo unknown)"
'''
                script {
                    // 让后续所有 sh 都能直接用 uv（若节点本身没有）
                    env.PATH = "${env.CI_TOOLS}:${env.PATH}"
                }
                sh 'mkdir -p "$UV_CACHE_DIR" "$PIP_CACHE_DIR" reports'
            }
        }

        // --------------------------------------------------------------------
        stage('③ 后端测试') {
            when { expression { !params.SKIP_TESTS } }
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail

# 起 CI 专用 Broker：4 条集成用例靠它从 skipped 变成真正跑通
docker rm -f "$BROKER_CT" >/dev/null 2>&1 || true
docker run -d --name "$BROKER_CT" \
  -p "127.0.0.1:${CI_MQTT_PORT}:1883" \
  -v "$WORKSPACE/deploy/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  "$BROKER_IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if python3 -c "import socket;socket.create_connection(('127.0.0.1',${CI_MQTT_PORT}),2)" 2>/dev/null; then
    echo ">> CI Broker 就绪：$CI_MQTT_URL"
    break
  fi
  sleep 1
done
python3 -c "import socket;socket.create_connection(('127.0.0.1',${CI_MQTT_PORT}),2)" \
  || { echo "!! CI Broker 未就绪"; exit 1; }
'''
                sh 'uv sync --all-packages'
                sh '''#!/usr/bin/env bash
set -euo pipefail
# KK_IT_MQTT_URL：集成用例读这个（默认 127.0.0.1:1883）；mqtt_e2e.py 读 KK_MQTT_URL
export KK_IT_MQTT_URL="$CI_MQTT_URL"
export KK_MQTT_URL="$CI_MQTT_URL"
# 有 Broker 时应当是「全量通过」，而不是 198 passed + 4 skipped
.venv/bin/python -m pytest agent/tests server/tests -q \
  --junitxml=reports/pytest.xml --tb=short -p no:cacheprovider \
  | tee reports/pytest.log
'''
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'reports/pytest.xml'
                    sh 'docker rm -f "$BROKER_CT" >/dev/null 2>&1 || true'
                }
            }
        }

        // --------------------------------------------------------------------
        stage('④ Broker 端到端冒烟') {
            when { expression { !params.SKIP_TESTS } }
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail
# 单测证不到的语义：retain 只落 status、LWT 触发、离线命令由 Broker 排队、大输出分块重组。
# 复用仓库既有脚本，不另写一份 CI 版本。
docker rm -f "$BROKER_CT" >/dev/null 2>&1 || true
docker run -d --name "$BROKER_CT" \
  -p "127.0.0.1:${CI_MQTT_PORT}:1883" \
  -v "$WORKSPACE/deploy/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  "$BROKER_IMAGE" >/dev/null
for _ in $(seq 1 60); do
  python3 -c "import socket;socket.create_connection(('127.0.0.1',${CI_MQTT_PORT}),2)" 2>/dev/null && break
  sleep 1
done

# 脚本用 .venv/bin/kk-agent 起进程，所以必须用 venv 里的 python 执行
KK_MQTT_URL="$CI_MQTT_URL" .venv/bin/python scripts/mqtt_e2e.py | tee reports/mqtt_e2e.log
'''
            }
            post {
                always {
                    sh 'docker rm -f "$BROKER_CT" >/dev/null 2>&1 || true'
                }
            }
        }

        // --------------------------------------------------------------------
        stage('⑤ 前端构建与产物同步') {
            steps {
                dir('web') {
                    sh 'pnpm install --frozen-lockfile'
                    sh 'pnpm typecheck'
                    sh 'pnpm build'
                }
                sh '''#!/usr/bin/env bash
set -euo pipefail
# 前端产物随 kk-server 包分发（server/Dockerfile 里 COPY server/src），
# 所以镜像构建前必须把 web/dist 同步进包内目录，否则镜像里是上一次的旧 UI。
test -f web/dist/index.html || { echo "!! web/dist 未产出，pnpm build 是否失败？"; exit 1; }
rm -rf server/src/kk_server/web/*
cp -r web/dist/* server/src/kk_server/web/
echo ">> 已同步 $(find server/src/kk_server/web -type f | wc -l) 个文件到 server/src/kk_server/web/"
'''
                sh '''#!/usr/bin/env bash
set -euo pipefail
# 产物漂移检查：仓库里把构建产物也提交了（运维直接 docker build 就能用），
# 于是「改了前端忘同步」是会真实发生的静默问题——下一次别人构建得到的是旧 UI。
drift="$(git status --porcelain server/src/kk_server/web || true)"
if [ -z "$drift" ]; then
  echo ">> 前端产物与仓库已提交版本一致"
  exit 0
fi
printf '%s\n' "$drift" | tee reports/web-sync-drift.txt
if [ "$STRICT_WEB_SYNC" = "true" ]; then
  echo
  echo "!! 前端产物与仓库不一致（上方文件清单）。本地执行以下命令修正后提交："
  echo "     cd web && pnpm install && pnpm build"
  echo "     rm -rf ../server/src/kk_server/web/* && cp -r dist/* ../server/src/kk_server/web/"
  echo "   （若确认是工具链哈希差异而非内容漂移，可把 STRICT_WEB_SYNC 置 false 重跑）"
  exit 1
fi
echo ">> STRICT_WEB_SYNC=false：仅告警不失败"
'''
            }
        }

        // --------------------------------------------------------------------
        stage('⑥ Agent 二进制') {
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail
# 为什么单独建一个 venv：uv 建的 .venv 不带 pip，而 build_binary.sh 用
# `python -m pip install pyinstaller` 装依赖 —— 用 --seed 建带 pip 的构建 venv，
# 既不改仓库脚本，也不往节点全局环境装东西。
uv venv --seed --python 3.12 "$AGENT_BUILD_VENV"
uv pip install --python "$AGENT_BUILD_VENV/bin/python" --quiet paho-mqtt psutil "pyinstaller>=6.0"

PYINSTALLER_PYTHON="$AGENT_BUILD_VENV/bin/python" agent/build/build_binary.sh
test -f agent/dist/kk-agent || { echo "!! 未产出 agent/dist/kk-agent"; exit 1; }
ls -lh agent/dist/kk-agent
# 冒烟要用它真跑一遍，所以记下绝对路径
echo "$WORKSPACE/agent/dist/kk-agent" > .ci-tools/agent-bin.path
'''
            }
        }

        // --------------------------------------------------------------------
        stage('⑦ 构建服务端镜像') {
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail
# 构建上下文必须是仓库根（uv workspace 的锁在根 uv.lock）；.dockerignore 已排除 web/ 等大目录
docker build \
  -f server/Dockerfile \
  -t "$FULL_IMAGE" \
  --label "org.opencontainers.image.revision=$GIT_SHA" \
  --label "org.opencontainers.image.version=$IMAGE_TAG" \
  --label "org.opencontainers.image.source=https://github.com/taoweidong/KontainKeeper" \
  .

# 同步本地约定标签：deploy/offline/pack.sh 与 docker-compose.offline.yml 都用 kk-server:latest
docker tag "$FULL_IMAGE" "$OFFLINE_IMAGE"

docker image inspect "$FULL_IMAGE" \
  --format '>> 镜像 {{.RepoTags}} 大小 {{.Size}} 修订 {{index .Config.Labels "org.opencontainers.image.revision"}}'
'''
            }
        }

        // --------------------------------------------------------------------
        stage('⑧ 镜像部署冒烟') {
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail
# 真起 Broker + 真起这个镜像 + 真跑 Agent 二进制：
# 健康 → 登录 → 上线 → 指标 → 命令 → 结果 → 审计。跑不过就不许推送、不许部署。
AGENT_BIN_PATH="$(cat .ci-tools/agent-bin.path)"
scripts/ci_smoke.sh "$FULL_IMAGE" "$AGENT_BIN_PATH" \
  | tee reports/image-smoke.log
'''
            }
        }

        // --------------------------------------------------------------------
        stage('⑨ 推送镜像') {
            when { expression { return !(params.IMAGE_REGISTRY?.trim() ?: '').isEmpty() } }
            steps {
                withCredentials([usernamePassword(credentialsId: 'kk-registry-creds',
                                                  usernameVariable: 'REG_USER',
                                                  passwordVariable: 'REG_PASS')]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
printf '%s' "$REG_PASS" | docker login "$REGISTRY_HOST" -u "$REG_USER" --password-stdin
docker push "$FULL_IMAGE"
if [ "$ENVIRONMENT" = "production" ]; then
  docker tag "$FULL_IMAGE" "${REGISTRY}/${IMAGE_NAME}:latest"
  docker push "${REGISTRY}/${IMAGE_NAME}:latest"
fi
docker logout "$REGISTRY_HOST" >/dev/null 2>&1 || true
echo ">> 已推送 $FULL_IMAGE"
'''
                }
            }
        }

        // --------------------------------------------------------------------
        stage('⑩ 部署') {
            when { expression { return !params.SKIP_DEPLOY && !(params.DEPLOY_HOST?.trim() ?: '').isEmpty() } }
            steps {
                script {
                    if (params.ENVIRONMENT == 'production' && !params.AUTO_APPROVE) {
                        input message: "确认部署到生产环境？\n镜像：${env.FULL_IMAGE}\n目标：${params.DEPLOY_USER}@${params.DEPLOY_HOST}:${params.DEPLOY_DIR}",
                              ok: '确认部署'
                    }
                }
                withCredentials([sshUserPrivateKey(credentialsId: 'kk-deploy-ssh',
                                                   keyFileVariable: 'DEPLOY_KEY'),
                                 string(credentialsId: 'kk-agent-ips', variable: 'KK_AGENT_IPS_VAL'),
                                 string(credentialsId: 'kk-admin-pass', variable: 'KK_ADMIN_PASS_VAL')]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
TARGET="$DEPLOY_USER@$DEPLOY_HOST"
remote() { ssh $SSH_OPTS -i "$DEPLOY_KEY" "$TARGET" "$@"; }

# 1) 目标机 .env：凭据只经 stdin 写入，不落 CI 工作区、不进构建日志（仓库公开，.env 严禁入库）
{
  printf 'KK_AGENT_IPS=%s\n' "$KK_AGENT_IPS_VAL"
  printf 'KK_ADMIN_USER=%s\n' "$ADMIN_USER"
  printf 'KK_ADMIN_PASS=%s\n' "$KK_ADMIN_PASS_VAL"
  printf 'KK_TOPIC_PREFIX=%s\n' "$TOPIC_PREFIX"
  # 用镜像仓库时必须显式指定待部署镜像（prod compose 的 image 支持该变量）
  [ -n "$REGISTRY" ] && printf 'KK_SERVER_IMAGE=%s\n' "$FULL_IMAGE"
} | remote "umask 077; mkdir -p '$DEPLOY_DIR'; cat > '$DEPLOY_DIR/.env'"
echo ">> 已写入目标机 .env（未回显内容）"
'''
                    sh '''#!/usr/bin/env bash
set -euo pipefail
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
TARGET="$DEPLOY_USER@$DEPLOY_HOST"
remote() { ssh $SSH_OPTS -i "$DEPLOY_KEY" "$TARGET" "$@"; }

# 2) 同步代码到目标机：compose 文件、mosquitto.conf 必须与本次提交一致，
#    否则「代码回滚了、配置没回滚」这类漂移无法避免。
#    .env 是未跟踪文件，reset --hard 不会动它。
remote "set -e; cd '$DEPLOY_DIR'; git fetch --quiet --all --prune; git reset --hard --quiet '$GIT_SHA'; git log -1 --oneline"
'''
                    sh '''#!/usr/bin/env bash
set -euo pipefail
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
TARGET="$DEPLOY_USER@$DEPLOY_HOST"
remote() { ssh $SSH_OPTS -i "$DEPLOY_KEY" "$TARGET" "$@"; }

# 3) 目标机拉取镜像并滚动重启；无仓库时退回本地构建（--build）
if [ -n "$REGISTRY" ]; then
  remote "set -e; cd '$DEPLOY_DIR';
          docker compose -f docker-compose.prod.yml --env-file .env pull;
          docker compose -f docker-compose.prod.yml --env-file .env up -d --remove-orphans;
          docker compose -f docker-compose.prod.yml --env-file .env ps"
else
  echo ">> 未配置 IMAGE_REGISTRY：在目标机本地构建（较慢，适合内网/单机场景）"
  remote "set -e; cd '$DEPLOY_DIR';
          docker compose -f docker-compose.prod.yml --env-file .env up -d --build --remove-orphans;
          docker compose -f docker-compose.prod.yml --env-file .env ps"
fi
'''
                    sh '''#!/usr/bin/env bash
set -euo pipefail
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
# 4) 清掉本地构建产生的悬空中间层，避免构建节点磁盘被反复构建撑满
docker image prune -f --filter "label=org.opencontainers.image.revision=$GIT_SHA" >/dev/null 2>&1 || true
'''
                }
            }
        }

        // --------------------------------------------------------------------
        stage('⑪ 部署验证') {
            when { expression { return !params.SKIP_DEPLOY && !(params.DEPLOY_HOST?.trim() ?: '').isEmpty() } }
            steps {
                withCredentials([string(credentialsId: 'kk-admin-pass', variable: 'KK_ADMIN_PASS_VAL')]) {
                    sh '''#!/usr/bin/env bash
set -euo pipefail
echo ">> 探活：$DEPLOY_HEALTH"
health=""
for _ in $(seq 1 30); do
  health="$(curl -fsS --max-time 8 "$DEPLOY_HEALTH/api/health" 2>/dev/null || true)"
  [ -n "$health" ] && break
  sleep 4
done
[ -n "$health" ] || { echo "!! 部署后健康检查失败：$DEPLOY_HEALTH/api/health 无响应"; exit 1; }
echo "$health" | python3 -m json.tool

# 断言：ok=true 且 Broker 已连上（起得来但连不上 Broker 是最常见的「假成功」）
ok="$(printf '%s' "$health" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ok"))')"
broker="$(printf '%s' "$health" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("broker"))')"
if [ "$ok" != "True" ] || [ "$broker" != "connected" ]; then
  echo "!! 部署验证未通过：ok=$ok broker=$broker"
  exit 1
fi
echo ">> 服务端就绪：ok=$ok broker=$broker"

# 登录一次并读 stats，确认鉴权与数据链路都活着（不只是进程起来了）。
# 口令走 stdin 进 curl（--data @-），避免出现在进程参数里。
# 注意：同一用户名连续失败 5 次会锁定 300 秒 —— 凭据配错时别连续重跑本 stage。
token="$(printf '{"username":"%s","password":"%s"}' "$ADMIN_USER" "$KK_ADMIN_PASS_VAL" | \
  curl -fsS --max-time 10 -X POST "$DEPLOY_HEALTH/api/login" \
    -H 'Content-Type: application/json' --data @- \
  | python3 -c 'import json,sys;print(json.load(sys.stdin).get("token",""))')"
[ -n "$token" ] || { echo "!! 部署验证未通过：管理员登录失败（检查 kk-admin-pass 凭据与目标机 .env）"; exit 1; }

curl -fsS --max-time 10 -H "Authorization: Bearer $token" "$DEPLOY_HEALTH/api/system/stats" \
  | python3 -m json.tool
echo ">> 部署验证通过：健康 / Broker / 登录 / stats 全通"
'''
                }
            }
        }

        // --------------------------------------------------------------------
        stage('⑫ 离线包（可选）') {
            when { expression { params.PACK_OFFLINE } }
            steps {
                sh '''#!/usr/bin/env bash
set -euo pipefail
# 内网无网部署用：把 kk-server / Broker / 基础镜像打成 tar。
# 走仓库既有脚本（manifest.txt 是单一事实源），不在这里另写一份清单。
./deploy/offline/pack.sh
ls -lh deploy/offline/images
'''
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true, fingerprint: true
            sh 'docker rm -f "$BROKER_CT" >/dev/null 2>&1 || true'
        }
        success {
            script {
                if (env.OFFLINE_IMAGE && fileExists('agent/dist/kk-agent')) {
                    archiveArtifacts artifacts: 'agent/dist/kk-agent', allowEmptyArchive: true
                }
            }
            echo "构建成功：${env.FULL_IMAGE}"
        }
        failure {
            echo """构建失败。排查顺序见 docs/ci-jenkins.md §6：
      1. 后端测试 reports/pytest.log（集成用例 skip 说明 CI Broker 没起来）
      2. 镜像冒烟 reports/image-smoke.log（含容器日志尾部）
      3. Broker 语义冒烟 reports/mqtt_e2e.log
      4. 前端产物漂移 reports/web-sync-drift.txt"""
        }
        cleanup {
            // 兜底清理：冒烟脚本自己会收尾，但构建被中断时容器会残留（名字带随机后缀）
            sh '''#!/usr/bin/env bash
docker rm -f "$BROKER_CT" >/dev/null 2>&1 || true
docker ps -aq --filter "name=kk-smoke-" | xargs -r docker rm -f >/dev/null 2>&1 || true
docker network ls -q --filter "name=kk-smoke-net-" | xargs -r docker network rm >/dev/null 2>&1 || true
exit 0
'''
            // 保留 .ci-cache（uv/pip 缓存）与 .ci-tools（uv + 构建 venv），下次构建直接复用
        }
    }
}
