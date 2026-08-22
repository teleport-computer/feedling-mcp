"""Agent-protocol primitives shared by chat and memory — pure stdlib, zero io deps.

## 为什么单独成包（而不是留在 ``core/``）

这两个模块（``protocol_leak`` / ``self_thinking``）是 Memory Garden 内核唯一依赖的
非标准库代码。内核要能独立发布，它们就必须跟着一起发布 —— 留在 io 的 ``core/`` 里
的话，外部使用者装了 memgarden 会 import 不到。

**包名与独立发布时的包名一致**，所以内核代码两边写法相同，不用维护 import 分叉。
io 现在用的是本目录这份源码；接入独立库之后，删掉本目录、装同名的包即可，
内核和调用方一行都不用改。

⚠️ 这里只放**与业务无关、两条 runtime 都要用**的协议原语。io 专有的东西
（信封、enclave、限流、store…）仍在 ``core/``，那些不会被发布出去。
"""
