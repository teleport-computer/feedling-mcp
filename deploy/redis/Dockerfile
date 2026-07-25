# deploy/redis/Dockerfile — TEE Redis 服务端。
# 官方镜像原封不动（已实测 8.8.0 编译带 TLS），只叠加我们的配置与 entrypoint。
FROM redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005

COPY redis/redis.conf /etc/redis/redis.conf
COPY redis/entrypoint-wrapper.sh /usr/local/bin/entrypoint-wrapper.sh
RUN chmod +x /usr/local/bin/entrypoint-wrapper.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
