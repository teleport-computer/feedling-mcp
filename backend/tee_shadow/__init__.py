"""TEE 影子写入路径（spec §5.1）。

主 Postgres 写入点在 dual-write 灰度期间尽力而为地把同一份写入镜像到 TEE 影
子库；镜像失败绝不传染主路径，只记录失败计数供 reconciler 事后补偿。见
`tee_shadow.mirror`。
"""
