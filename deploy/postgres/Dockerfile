# deploy/postgres/Dockerfile
# postgres:17 digest 待实施时解析：docker manifest inspect postgres:17 取 amd64 digest 后钉死
FROM postgres:17@sha256:9c1534cbf839ec70409508a874f4c02bf4739de2f32f77efe080eef1cfd34bf4

# GHCR 按这个 label 把 package 关联回仓库。缺了它，package 只属于当初手工
# `docker push` 的那个人，Actions 的 GITHUB_TOKEN 推它会 permission_denied:
# write_package（2026-07-24 首次把 pg 部署搬上 CI 时实测撞到）。
LABEL org.opencontainers.image.source=https://github.com/teleport-computer/feedling-mcp

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl jq cron \
    && rm -rf /var/lib/apt/lists/*

# WAL-G：v3.0.8 起步；实施时核验 release notes 支持 PG17，不支持则升到最低支持版并回填此处
ARG WALG_VERSION=v3.0.8
RUN curl -fsSL "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/wal-g-pg-22.04-amd64.tar.gz" \
    | tar xz -C /usr/local/bin/ \
    && mv /usr/local/bin/wal-g-pg-22.04-amd64 /usr/local/bin/wal-g \
    && chmod +x /usr/local/bin/wal-g

ENV AWS_S3_FORCE_PATH_STYLE="true"
ENV AWS_REGION="auto"

# cron 自己的 PATH 默认是 /usr/bin:/bin，不含 wal-g 所在的 /usr/local/bin。
# backup-push.sh 里已经 export 了 PATH，这里再用 crontab 原生的 PATH= 环境行兜一道：
# 2026-07-23 prod/test 双双回退到一个早于 PATH 修复的镜像 tag，当晚 03:00 的 base
# backup 就死在 "wal-g: command not found"（26h 后才被 pg-monitor 的 BASE 陈旧断言
# 抓到）。脚本与 crontab 两层各自独立生效，回退掉任意一层都还有另一层顶着。
RUN printf '%s\n' \
      'PATH=/usr/local/bin:/usr/bin:/bin' \
      '0 3 * * * root /usr/local/bin/backup-push.sh >> /var/log/walg-backup.log 2>&1' \
      > /etc/cron.d/walg-backup \
    && chmod 0644 /etc/cron.d/walg-backup

COPY postgres/pg_hba.conf /etc/postgresql/pg_hba.conf
COPY postgres/backup-push.sh postgres/restore.sh postgres/ensure-roles.sh \
     postgres/entrypoint-wrapper.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup-push.sh /usr/local/bin/restore.sh \
    /usr/local/bin/ensure-roles.sh /usr/local/bin/entrypoint-wrapper.sh
# 构建期断言：PATH 修复（463f6b7b）曾在一次"从 docker ps 抄现役 tag"的重部署里被
# 悄悄带走，且只有等到次日 03:00 备份失败才暴露。让镜像根本构建不出来，比等监控报警快。
RUN grep -q '^export PATH=.*/usr/local/bin' /usr/local/bin/backup-push.sh \
    || { echo "FATAL: backup-push.sh lost its PATH export — cron 会 wal-g: command not found"; exit 1; }

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
CMD ["postgres"]
