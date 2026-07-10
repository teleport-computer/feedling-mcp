# deploy/postgres/Dockerfile
# postgres:17 digest 待实施时解析：docker manifest inspect postgres:17 取 amd64 digest 后钉死
FROM postgres:17@sha256:9c1534cbf839ec70409508a874f4c02bf4739de2f32f77efe080eef1cfd34bf4

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

RUN echo "0 3 * * * root /usr/local/bin/backup-push.sh >> /var/log/walg-backup.log 2>&1" > /etc/cron.d/walg-backup \
    && chmod 0644 /etc/cron.d/walg-backup

COPY postgres/pg_hba.conf /etc/postgresql/pg_hba.conf
COPY postgres/backup-push.sh postgres/restore.sh postgres/ensure-roles.sh \
     postgres/entrypoint-wrapper.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/backup-push.sh /usr/local/bin/restore.sh \
    /usr/local/bin/ensure-roles.sh /usr/local/bin/entrypoint-wrapper.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-wrapper.sh"]
CMD ["postgres"]
