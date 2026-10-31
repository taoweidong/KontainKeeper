# KontainKeeper MQTT Broker 镜像（ubuntu 基底自建）
#
# 生产环境镜像清单只提供：python-3.12 / node-20 / nginx-1.20 / postgres:18.6 /
# redis-7.2.11 / mongo-8.2.3 / ubuntu_latest——不含 eclipse-mosquitto。
# 因此 Broker 统一在本清单内构建：ubuntu:latest + apt 源的 mosquitto 2.0.x，
# 满足架构要求的 Mosquitto 2.x 语义（匿名开放 / LWT / retain 持久化 / QoS1 离线队列）。
#
# 构建期需要 apt 源可达（与 kk-server 构建期需要 pip 源同级别的外网要求）；
# 内网无网环境用离线包里的 kk-broker tar（见 docker/offline/README.md）。
#
# 目录约定与 eclipse-mosquitto 镜像保持一致：/mosquitto/config/mosquitto.conf +
# /mosquitto/data——compose 与 ci_smoke.sh 的挂载方式一个字节不用改，只换 image 引用。
ARG UBUNTU_BASE=ubuntu:latest
FROM ${UBUNTU_BASE}

# mosquitto-clients 带上：部署验证（docs/deployment.md §3.2）要在容器里跑 mosquitto_sub
RUN apt-get update \
    && apt-get install -y --no-install-recommends mosquitto mosquitto-clients \
    && rm -rf /var/lib/apt/lists/*

# 数据目录预建并交给 mosquitto 用户（root 启动后 mosquitto 自行降权到该用户，
# persistence_location /mosquitto/data/ 需要它可写；compose 的命名卷会继承此属主）
RUN mkdir -p /mosquitto/config /mosquitto/data \
    && chown -R mosquitto:mosquitto /mosquitto

# 数据卷属主归位（eclipse 时代旧卷 uid=1883 ≠ ubuntu mosquitto uid），再 exec 原命令
COPY docker/broker/broker-entrypoint.sh /usr/local/bin/broker-entrypoint
RUN chmod +x /usr/local/bin/broker-entrypoint
ENTRYPOINT ["/usr/local/bin/broker-entrypoint"]

EXPOSE 1883

CMD ["mosquitto", "-c", "/mosquitto/config/mosquitto.conf"]
