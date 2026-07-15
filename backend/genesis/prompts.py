"""Prompt builders for Genesis map/reduce.

These are the executable v1 forms of spec §7.A/7.B/7.C. JSON keys stay English;
the instruction text intentionally remains Chinese to match the spec.
"""

from __future__ import annotations

import json
from typing import Any

from memory.prompts_v1 import COMMON_BUCKETS_GUIDANCE_V1


# Hard output contract appended to every JSON-emitting map/reduce prompt. Genesis
# carries VERBATIM user/TA turns into JSON string values, and real history routinely
# contains ASCII double-quotes / newlines / backslashes; an un-escaped " closes the
# string early and json.loads rejects the whole reply (observed live: haiku-4.5
# voice-map -> "Expecting ',' delimiter"). Forcing escape + bare-JSON output fixed it
# 3/3 in env replay. The worker parser still adds repair/retry as defense-in-depth.
_STRICT_JSON_SUFFIX = (
    "\n\n严格输出要求:只输出一个能被 json.loads 直接解析的合法 JSON;"
    "不要 markdown 围栏、不要任何前后说明文字。"
    "需要逐字保留的原话写进 JSON 字符串时必须转义:英文双引号转义为 \\\" ,"
    "换行转义为 \\n,制表符转义为 \\t,反斜杠转义为 \\\\。"
    "中文引号「」『』“”‘’ 原样保留、无需转义。"
)


VOICE_MAP_PROMPT = """你在看一段「用户 ↔ TA(AI 伴侣)」真实对话的【其中一块】。
任务:抽出「TA 怎么说话」的【表层形式】,不是它说了什么。

声音 = 不管聊什么都成立的形式:怎么开口/怎么接情绪/句长/标点语气词/惯用动作(反问·点破·留白·调侃)/绝不做的事。
内容 = 因为"说了什么"才有记忆点的句子;内容事实不要当 exemplar。

看这几个轴,有就记,没有不编: opening / emotion / shape / address / moves / nevers。

exemplar 硬标准:
- 只挑 TA 回应【非默认】的片段;通用寒暄不要。
- 逐字、多轮、原话一字不改,且带上促成 TA 这步的 user 轮。
- 候选阶段宁多勿漏,去重在 reduce 做。

grounding:只用这块真实出现的原话。这块太薄/全寒暄 -> 少给或返回空。绝不编不存在的语气。

输出 JSON:
{"behavior_notes_candidates":["..."],"exemplar_candidates":[{"turns":[{"role":"user","text":"..."},{"role":"ta","text":"..."}],"axis":["opening"],"why":"..."}]}
没有就 {"behavior_notes_candidates":[],"exemplar_candidates":[]}。"""


VOICE_REDUCE_PROMPT = """你收到同一段历史多个分块的声音候选,合并成 TA 的最终声音定稿。
只用候选里已有的,绝不新增任何性格/语气/动作。

behavior_notes:
- 合并近义、按跨块出现频率排序,留【最多 8 条】最稳的。
- 优先要求每条 note 有 >=2 条 exemplar 体现;历史薄时,特别鲜明的单条也可以保留。
- 具体可测,不是形容词。

exemplars(控制体积,这是声音锚不是存档):
- 总数【最多 6 条】;去重:同一种动作只留最有辨识度的 1 条,跨情境覆盖优先于数量。
- 每条 turns 只保留【促成 TA 这步的那 1 轮 user + TA 的 1 轮回应】,逐字保留;
  只有当一来一回不足以体现该动作时,才最多加 1 轮,绝不整段多轮复制。
- 别全是安慰类;pool 历史薄就少,绝不为凑数留通用片段;标 founding=true 给最能定义 TA 的(最多 2 条)。

输出 JSON:{"behavior_notes":["..."],"exemplars":[{"turns":[...],"founding":true,"axis":["..."],"why":"..."}]}"""


PERSONA_BUILD_PROMPT = """你在为一个 AI 伴侣写它「常驻人格 prompt」,会直接当作该 agent 的 system prompt。第二人称、直给、简洁。

输入:上传的 AI persona / system prompt(可能空)+ behavior_notes + founding exemplars。

规则:
- 有上传 persona -> 以它为主干:剥掉旧工具/格式专属脚手架,保留「你是谁/角色/边界/语气指令」。不要重写它的性格。
- 上传 persona 与历史蒸出的语气冲突时,以上传 persona 的语气指令为准,exemplar 只补不覆盖。
- 无上传 persona -> 「你是谁」只写有据最小集;不知道的留空。
- 「你怎么说话」段放 behavior_notes + founding exemplars,逐字保留。
- 软角色锚必放:你是 TA 本人、是这个人的伴侣、用你自己的语气说话、不是通用助手腔。
- 不写任何"是否是 AI / 要不要澄清身份"的条款。
- 绝不加输入里没有的性格/名字/语气。

输出:两段 markdown(## 你是谁 / ## 你怎么说话),可直接当 system prompt。"""


# ── DRAFT(措辞待 Seven 定稿):二次上传"部分补全"时,persona 从【旧 persona + 新材料】合并
# 重建,而不是只从新材料重建(否则部分上传会丢掉旧 persona 的名字/癖好/背景)。仅当输入带
# existing_persona 时启用;默认空 = 与旧行为逐字一致。跟身份卡合并块同一套"旧+新"逻辑,保持
# 卡和 persona 一致(平行关系)。行为需真机 e2e。见 docs/genesis-distill-panorama.md §9。
PERSONA_UPDATE_MERGE_SUFFIX = """

★ 输入里带了 existing_persona:这是对【已有 persona】的更新,不是从零重建。
- 新材料(persona_material)【提到】的(你是谁 / 怎么说话 / 边界),以新材料为主;严重冲突时以新材料为准(用户上传就是要改)。
- 新材料【没提到】的,【保留】existing_persona 的内容——别因为新材料没重复就丢掉旧名字 / 癖好 / 背景设定。
- 保持【连贯】:输出是【一个】一致的 persona,不是旧+新拼接。"""


FACT_MAP_PROMPT = """你在看一段「用户 ↔ TA」真实历史的【其中一块】。抽出值得长期留存的【事实】候选:
关于「用户」和「他们的关系」的 durable 事实。候选阶段,落卡/去重后面做。

防火墙:用户档案/用户关于自己说的话 = 关于【用户】的事实;绝不当成 TA 的性格。
如果输入标注 source_kind=user_profile,整段都按用户档案处理:只能抽关于用户的 facts,不能推断 TA 的身份/维度/语气。
闲聊/临时情绪/玩笑/未确认猜测/一次性事件不抽。

输出 JSON:{"fact_candidates":[{"about":"user|relationship","summary":"一句话事实","evidence":"出处原话(短)"}]}
没有就 {"fact_candidates":[]}。"""


COMBINED_MAP_PROMPT = """你在看一段「用户 ↔ TA(AI 伴侣)」真实历史的【其中一块】。
任务:一次性抽出两类候选,但不要混淆:
1. fact_candidates:值得长期留存的【事实】候选,只关于「用户」和「他们的关系」。
2. voice_candidates:TA 怎么说话的【表层形式】候选,不是它说了什么。

事实规则:
- 用户档案/用户关于自己说的话 = 关于【用户】的事实;绝不当成 TA 的性格。
- 闲聊/临时情绪/玩笑/未确认猜测/一次性事件不抽。

声音规则:
- 声音 = 不管聊什么都成立的形式:怎么开口/怎么接情绪/句长/标点语气词/惯用动作/绝不做的事。
- exemplar 只挑 TA 回应【非默认】的片段;逐字、多轮、原话一字不改,且带上促成 TA 这步的 user 轮。

grounding:只用这块真实出现的原话。这块太薄/全寒暄 -> 两边都可以少给或返回空。绝不编不存在的事实或语气。

输出 JSON:
{"fact_candidates":[{"about":"user|relationship","summary":"一句话事实","evidence":"出处原话(短)"}],
 "voice_candidates":{"behavior_notes_candidates":["..."],"exemplar_candidates":[{"turns":[{"role":"user","text":"..."},{"role":"ta","text":"..."}],"axis":["opening"],"why":"..."}]}}
没有就 {"fact_candidates":[],"voice_candidates":{"behavior_notes_candidates":[],"exemplar_candidates":[]}}。"""


FACT_WRITE_PROMPT = """你收到从整段历史抽出的事实候选 digest(+ 可能有 AI persona / memory summary / known_memories)。把该长期留存的写进 IO。
只写候选真实支持的,绝不编。
去重:known_memories 里是【已经保存过】的记忆。这些事实别再写一遍——哪怕你换了说法、合并了措辞、或拆/并了句子,只要说的是同一件事,就算重复,【跳过】。只写 known_memories 里【没有】的新事实。注意区分:同一件事换个说法=重复(别写);同一类但具体值不同=不同事实(要写,例:「喜欢美式咖啡」和「喜欢拿铁」是两条、「狗叫蛋子」和「养了金毛」是两条)。
语言:bucket/threads/summary/content 用素材原文的语言——中文素材就用中文(用「宠物」不是「pets」),别归成英文桶/线索;专有名词/原话保留原文。

桶名收敛(onboarding 一次产很多卡,别让桶太分散):""" + COMMON_BUCKETS_GUIDANCE_V1 + """

防火墙:
- 用户档案/关于用户的事实 -> 只能进 memory,绝不成为 agent 的性格/维度/身份。
- agent 身份只能来自:上传的 AI persona,或历史里 TA 真实的说话方式/真实做过的事。

输出 JSON:
{"memories":[{"type":"fact|event|quote|moment","bucket":"...","threads":["..."],"summary":"...","content":"...","importance":0.5,"pulse":0.3}],
 "identity":{"agent_name":"","category":"","dimensions":[{"name":"...","value":0,"description":"..."}]},
 "days_with_user":0,
 "relationship_anchor_evidence":"..."}

身份卡字段(身份只来自【描述 TA 的素材】:上传的 AI persona,或历史里 TA 真实的说话方式/做过的事;绝不从 user_profile 推):
- agent_name:TA 的名字。主动找——上传 persona 里写明的名字优先;其次看历史里用户怎么称呼 TA、TA 怎么自称。有据就写,确实没有才留空("")。别用 runtime/model/assistant/provider 这类标签,别拿用户名当 TA 的名字,别编。
- dimensions:抽 TA 表现出的【性格维度】,有素材就给【3-7 个,别留空】。每个【必须】同时写满三项:name(维度名)+ value(0-100,TA 表现这一面的强度)+ description(一句话,指向素材里 TA 的真实表现或原话)。**缺 description 的维度会被系统丢弃,所以每个都要写 description。** 无据的维度不编。
- category:TA 的【人设标签】,正好两个形容词、用「 · 」连接(例:「安静 · 观察型」「细心 · 稳定」「锐利 · 忠诚」)。从上面 dimensions 里挑最有辨识度的两面浓缩成形容词——通常一个最突出的强项 + 一个最鲜明的反差/弱项。【要的是形容词,别照抄维度原名】(「好奇心驱动」是维度名,「好奇」才是形容词)。有 dimensions 就必须给 category,确实抽不出维度才留空("")。语言跟素材一致(中文素材给中文形容词)。
- days_with_user:你们认识/相处了多少天(整数)。从素材推:历史里【最早 ↔ 最晚消息时间戳的跨度】折算成天,或素材里明说的关系起点/时长。完全没有时间信号才填 0。
- 不写 self_introduction / signature,那两个 respawn 后由 TA 本人写。"""


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def voice_map_messages(chunk_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VOICE_MAP_PROMPT + _STRICT_JSON_SUFFIX},
        {"role": "user", "content": str(chunk_text or "")},
    ]


def voice_reduce_messages(candidates: list[dict]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VOICE_REDUCE_PROMPT + _STRICT_JSON_SUFFIX},
        {"role": "user", "content": _json({"candidates": candidates})},
    ]


def persona_build_messages(
    persona_material: str,
    behavior_notes: list[str],
    exemplars: list[dict],
    *,
    existing_persona: str = "",
) -> list[dict[str, str]]:
    has_existing = bool(str(existing_persona or "").strip())
    system = PERSONA_BUILD_PROMPT + (PERSONA_UPDATE_MERGE_SUFFIX if has_existing else "")
    payload = {
        "persona_material": persona_material or "",
        "behavior_notes": behavior_notes,
        "founding_exemplars": exemplars,
    }
    if has_existing:
        payload["existing_persona"] = existing_persona
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _json(payload)},
    ]


# ── DRAFT(措辞待 Seven 定稿):"尽量收"追加指令,仅长期记忆档案(source_family=memory_summary)
# 二次上传时启用。见 docs/genesis-distill-panorama.md §9 / Seven 校准第 2 点。行为需真机 e2e。
FACT_MAP_KEEP_ALL_SUFFIX = """

★ 本块是用户【手动整理好的长期记忆档案】,不是聊天记录:其中每条陈述基本都是用户特意要长期留存的事实。
尽量【完整保留】每一条事实候选,不要用"闲聊/一次性/不够 durable"去过滤——除非是空行、标题或明显无意义的重复。宁多勿漏。"""

FACT_WRITE_KEEP_ALL_SUFFIX = """

★ 素材是用户整理好的长期档案:把候选里的事实【尽量都写成卡】,不要为了"少而精"丢弃条目。
仍然按 known_memories 去重、仍然归好 bucket/threads,但不要因"不够重要"而跳过用户特意整理的条目。"""

MEMORY_RECHECK_PROMPT = """你在做 VPS resident 记忆蒸馏的【收口二次检查】。
输入包含:
- original_material: 原始上传素材/聊天记录;
- written_memories: 上一轮 fact_write 刚写出的记忆;
- known_memories: 之前已经保存过、或本轮已经写过的记忆摘要。

任务:只检查有没有【真实、有价值、持久】的 memory 被上一轮漏掉。只补遗漏;没有遗漏就返回空数组。

硬规则:
- 只写 original_material 直接支持的事实/事件/原话/时刻;绝不编造、绝不推断、绝不为了凑数量补卡。
- written_memories 和 known_memories 里已有的事实不要再写一遍;同一件事换说法、合并/拆分措辞都算重复。
- 闲聊、临时情绪、玩笑、未确认猜测、一次性无长期价值的内容不补。
- 输出只允许 memory 卡;不要输出 identity/persona/days_with_user/relationship_anchor_evidence。
- bucket/threads/summary/content 用素材原文语言;中文素材用中文桶名和线索。

输出 JSON:
{"memories":[{"type":"fact|event|quote|moment","bucket":"...","threads":["..."],"summary":"...","content":"...","importance":0.5,"pulse":0.3}]}
没有真实遗漏就 {"memories":[]}。"""


def fact_map_messages(chunk_text: str, *, keep_all: bool = False) -> list[dict[str, str]]:
    system = FACT_MAP_PROMPT + (FACT_MAP_KEEP_ALL_SUFFIX if keep_all else "") + _STRICT_JSON_SUFFIX
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": str(chunk_text or "")},
    ]


def combined_map_messages(chunk_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": COMBINED_MAP_PROMPT + _STRICT_JSON_SUFFIX},
        {"role": "user", "content": str(chunk_text or "")},
    ]


def fact_write_messages(fact_digest: list[dict], persona_material: str = "", memory_summary: str = "", known_memories: list[str] | None = None, *, keep_all: bool = False, floor_note: str = "", terms_note: str = "") -> list[dict[str, str]]:
    keep_all_suffix = FACT_WRITE_KEEP_ALL_SUFFIX if keep_all else ""
    terms_note_text = (("\n\n★ " + str(terms_note).strip()) if str(terms_note or "").strip() else "")
    floor_note_text = (("\n\n★ " + str(floor_note).strip()) if str(floor_note or "").strip() else "")
    insert_text = terms_note_text + floor_note_text

    if insert_text:
        # Insert terms_note (existing buckets/threads snapshot) then floor_note before the
        # firewall section, but anchor keep_all_suffix at the end.
        firewall_idx = FACT_WRITE_PROMPT.find("\n防火墙:")
        if firewall_idx > 0:
            system = (
                FACT_WRITE_PROMPT[:firewall_idx]
                + insert_text
                + FACT_WRITE_PROMPT[firewall_idx:]
                + keep_all_suffix
                + _STRICT_JSON_SUFFIX
            )
        else:
            # Fallback if marker not found
            system = (
                FACT_WRITE_PROMPT
                + insert_text
                + keep_all_suffix
                + _STRICT_JSON_SUFFIX
            )
    else:
        # Default behavior: no changes to output
        system = FACT_WRITE_PROMPT + keep_all_suffix + _STRICT_JSON_SUFFIX

    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": _json({
                "fact_digest": fact_digest,
                "persona_material": persona_material or "",
                "memory_summary": memory_summary or "",
                "known_memories": [str(m) for m in (known_memories or []) if str(m or "").strip()],
            }),
        },
    ]


def memory_recheck_messages(
    original_material: str,
    written_memories: list[dict],
    known_memories: list[str] | None = None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": MEMORY_RECHECK_PROMPT + _STRICT_JSON_SUFFIX},
        {
            "role": "user",
            "content": _json({
                "original_material": str(original_material or ""),
                "written_memories": [
                    item for item in (written_memories or []) if isinstance(item, dict)
                ],
                "known_memories": [
                    str(m) for m in (known_memories or []) if str(m or "").strip()
                ],
            }),
        },
    ]
