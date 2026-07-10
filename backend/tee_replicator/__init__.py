"""RDS 密文内容 → TEE 明文内容的解密复制器（spec §5.2）。

reconciler（tee_shadow）搬运的是「明文运维表」——RDS 里本就是明文的行，逐字节
镜像即可。本包处理的是相反的一半：RDS 里以 v1 信封形式存储的**密文**内容
（chat_messages / memory_moments / world_book_entries / identity 信封），只有
enclave 能解密。replicator 逐行调 enclave 解密、剥掉信封加密学字段、把明文 doc
以 upsert 写进 TEE 明文库，用 (sort, id) 复合游标做只追加式增量扫描。

- transforms：纯函数密文 doc → 明文 doc（注入 decrypt 回调，便于测试）。
- worker：游标驱动的批处理，失败/local_only/dry_run/限速语义。
"""
