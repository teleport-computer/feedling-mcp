"""io 自己的记忆来源与写入模式清单 —— **不属于 Garden 内核**。

2026-08-17 从 `memory_garden/types.py` 搬回来。搬的理由很直接：
**内核一次都没有引用过它**，整个文件都是 io 的业务分类
（genesis_import / resident_absorb / model_api_capture / ombre_brain_sync …）。

内核真正需要的只有一个语义：**「这张卡是不是上次整理产出的」** ——
用来防止做梦的产物自己喂自己（整理完立刻又满足「攒够了」，无限循环）。
那一个语义留在 `memory_garden/dreaming.py` 里，不需要知道 io 的 17 个来源名。

留在这里之后：外部使用者接 Garden 时写 `source="slack_import"` 不会再被拒 ——
校验依据是宿主自己的清单，不是内核写死的枚举。
"""


MEMORY_SOURCE_VALUES = frozenset({
    "bootstrap",
    "chat",
    "genesis_import",
    "genesis_resident_distill",
    "history_import",
    "hosted_runtime_state",
    "live_conversation",
    "memory_capture",
    "memory_dream",
    "memory_migrate",
    "model_api_capture",
    "model_api_correction",
    "model_api_repair",
    "ombre_brain_sync",
    "resident_absorb",
    "resident_patch",
})

MEMORY_CAPTURE_MODE_VALUES = frozenset({
    "agent_tool",
    "genesis_import",
    "genesis_resident_distill",
    "memory_capture",
    "memory_dream",
    "repair",
    "state",
})

RESIDENT_ABSORB_SOURCE = "resident_absorb"
RESIDENT_PATCH_SOURCE = "resident_patch"

# One supersede action holds the per-user Garden mutation lock while retiring
# every source card and writing one successor.  Keep that critical section and
# the provider-visible payload bounded.  Larger consolidations must continue in
# a later tool round using the returned successor id; they must never be
# silently truncated to this limit.
MAX_MEMORY_SUPERSEDE_TARGETS = 20
