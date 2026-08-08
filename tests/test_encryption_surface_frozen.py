# tests/test_encryption_surface_frozen.py
"""加密面冻结守卫（TEE 扶正计划 v6 Task 0.5）。

背景：v6 把内容加密从「人人强制」改为「按用户 content_encryption 偏好可选、
默认明文」。任何「强制 K_enclave」的约束或写入路径都会让**明文档用户**的数据
写不进去/读不出来，与默认明文直接冲突。V2 是活跃开发线，仍在往强制加密上加
东西（迁移 0043 的表级 CHECK、以及仍在增长的服务端封装点）。

本守卫**冻结继续扩张**：除已登记的基线外，新增任何强制 K_enclave 的 CHECK
断言、或新增服务端封装写入点，直接红——想加就必须先在计划 Task 2.2 登记。

冻结两个面（对应计划 Task 0.5 的两条 bullet）：
  1. **表级强制信封 CHECK**（`_scan_migrations`）——数据库层拒绝明文行的证据。
  2. **底层强制封装写入点**（`_scan_write_sites`）——直接调用
     `content_encryption.build_envelope`、绕过通用 shape router 的站点。安全的
     `_build_shared_envelope_for_store` 不计入：它已经按 effective preference 路由。

2026-07-28 接手补齐（原实现只覆盖了第 1 面的一部分）：
  - 扫描面从 `alembic` 扩到 **`alembic_tee`**：表同步工作流正在那条链上新建
    30+ 张表（`0004_full_table_alignment`），是最可能引入新强制约束的地方，
    原守卫对它完全是瞎的。
  - 单一字面量 MARKER 扩成 **4 种等价写法**：原来的 `? 'K_enclave'` 可以被
    `length(x->>'K_enclave') > 0`、`x->>'K_enclave' IS NOT NULL`、
    `jsonb_exists(x,'K_enclave')` 轻易绕过——而 0043 自己就同时用了前两种，
    即原守卫连基线文件里 4 处强制断言只认得出 2 处。
  - 补上第 2 面（写入点冻结），原实现完全缺失。
  - 只剥离**整行**注释：既避免文档性提及被误判为违规，又不会误伤 SQL 里的
    `#>` jsonb 操作符（整体剥离 `#` 之后的内容会把它截断成漏报）。

参照 `tests/test_no_flask_anywhere.py` 的守卫模式。
"""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).parent.parent / "backend"

# 两条迁移链都要扫：alembic = RDS 主链，alembic_tee = TEE 库链（表同步工作流
# 正在这条链上建表，新强制约束最可能从这里进来）。
VERSION_DIRS = (
    BACKEND / "alembic" / "versions",
    BACKEND / "alembic_tee" / "versions",
)

# 「该行必须携带 K_enclave 收件人」的四种等价 SQL 写法。语义相同——都会让
# 明文行插不进去——所以必须一并冻结，只堵一种等于没堵。
FORCED_ENVELOPE_PATTERNS = (
    re.compile(r"\?\s*'K_enclave'"),                              # doc ? 'K_enclave'
    re.compile(r"->>\s*'K_enclave'\s*IS\s+NOT\s+NULL", re.I),     # doc->>'K_enclave' IS NOT NULL
    re.compile(r"length\s*\(\s*[^)]*->>\s*'K_enclave'", re.I),    # length(doc->>'K_enclave') > 0
    re.compile(r"jsonb_exists\s*\([^)]*'K_enclave'", re.I),       # jsonb_exists(doc,'K_enclave')
)

# v6 冻结基线：唯一允许携带「表级强制 K_enclave 信封 CHECK」的迁移。
# 0043 的两个约束（ck_v2_trajectory_envelope / ck_v2_trajectory_review_envelope，
# 合计 4 处断言）是 Phase 2 Task 2.2 的待改造对象，暂列白名单。**不要**为通过
# CI 而随手往这里加文件——新增项必须先在计划 Task 2.2 登记为待改造。
ALLOWLISTED_MIGRATIONS = {
    "0043_v2_encrypted_trajectories.py",
    # 0072 是**放宽**：把 0043 的「必须是信封」改成「信封 OR 明文」，正是本守卫
    # 想要的方向。它之所以被扫到，只是因为放宽后的 CHECK 里仍保留了信封分支
    # （加密档用户的行仍须完整），而守卫做的是文本匹配、认不出 `OR 明文分支`。
    # 登记在此而非放松 pattern：pattern 一旦放松，真正的新增强制约束也会溜过去。
    "0072_relax_v2_envelope_shape.py",
    # Voice transcript archives use the same ciphertext-OR-plaintext predicate.
    # 0080 creates the runtime table and TEE 0012/0016 create or upgrade its
    # promoted-primary counterpart; all three retain an explicit plaintext OR
    # branch and are text-scanner false positives for the same reason as 0072.
    "0080_voice_transcripts.py",
    "0012_voice_transcripts.py",
    "0016_merge_image_voice_pre.py",
    # TEE 0013 restores the same primary-runtime CHECK after the plaintext
    # migration has completed.  Its predicate explicitly accepts the exact
    # plaintext body shape as an OR branch, just like RDS 0072; the scanner only
    # sees the encrypted branch's K_enclave tokens and cannot infer that widening.
    "0013_primary_runtime_contracts.py",
}

# 底层封装 primitive：直接调用就产生双收件人信封。通用
# `_build_shared_envelope_for_store` 已按 effective content_encryption 路由，不是
# 强制封装点；把它算进去会在每个正确接入的新功能上误报。
WRITE_SITE_RE = re.compile(r"(?<![\w.])build_envelope\s*\(")
_DEF_RE = re.compile(r"def\s+_?build_(?:shared_envelope_for_store|envelope)\s*\(")

# 强制 primitive 基线。新增内容写路径应接通用 shape router，不能直接扩大此清单。
WRITE_SITE_BASELINE = {
    "backend/accounts/accounts_core.py": 1,
    "backend/content/content_core.py": 1,
    "backend/core/envelope.py": 1,
    "backend/model_api_runtime/v2/extraction.py": 1,
}


def _strip_comment_lines(text: str) -> str:
    """只去掉整行 Python 注释。

    不做「`#` 之后一律截断」——SQL 里的 `#>` / `#>>` jsonb 操作符会被截掉，
    把真约束变成漏报。整行注释足以挡掉文档性提及（如 alembic_tee 的
    `0004_full_table_alignment` 在说明里解释「不带 K_enclave CHECK」）。
    """
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


def _scan_migrations(version_dirs=VERSION_DIRS,
                     allowlist=ALLOWLISTED_MIGRATIONS) -> dict[str, int]:
    """返回 {文件名: 强制信封断言数}，只含不在 allowlist 的文件。"""
    hits: dict[str, int] = {}
    for vd in version_dirs:
        if not vd.is_dir():
            continue
        for f in sorted(vd.glob("*.py")):
            if f.name in allowlist:
                continue
            src = _strip_comment_lines(f.read_text(encoding="utf-8"))
            n = sum(len(p.findall(src)) for p in FORCED_ENVELOPE_PATTERNS)
            if n:
                hits[f.name] = n
    return hits


def _scan_write_sites(backend: Path = BACKEND) -> dict[str, int]:
    """返回 {相对路径: 绕过 shape router 的底层封装调用点数}。"""
    hits: dict[str, int] = {}
    root = backend.parent
    for f in sorted(backend.rglob("*.py")):
        if "__pycache__" in f.parts or f.name == "content_encryption.py":
            continue
        src = _DEF_RE.sub("", _strip_comment_lines(f.read_text(encoding="utf-8")))
        n = len(WRITE_SITE_RE.findall(src))
        if n:
            hits[str(f.relative_to(root))] = n
    return hits


# ---------------------------------------------------------------- 面 1：CHECK

def test_no_new_forced_envelope_check():
    """新增任何未登记的强制 K_enclave CHECK → 红（两条迁移链都扫）。"""
    offenders = _scan_migrations()
    assert not offenders, (
        "检测到新增的『表级强制 K_enclave 信封 CHECK』，与 v6 默认明文冲突"
        "（明文档用户的行会插不进去）：\n"
        + "\n".join(f"  {name}: {n} 处" for name, n in offenders.items())
        + "\n\n若确属加密档必需：先在计划 "
        "docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md "
        "的 Task 2.2 登记为待改造，再把文件名加入 ALLOWLISTED_MIGRATIONS。"
        "\n新存储的正确做法：按 content_encryption 偏好支持明文/信封两形状，"
        "不加表级强制 CHECK。"
    )


def test_guard_patterns_still_match_baseline():
    """防守卫失效空转：0043 的已知强制断言必须仍被 4 个 pattern 命中。

    若 0043 被重构或 pattern 因格式变化不再匹配，守卫会静默放行所有违规
    （正是 test_no_flask 注释警惕的 "silently pass"）。此测试是守卫的守卫。
    """
    baseline = BACKEND / "alembic" / "versions" / "0043_v2_encrypted_trajectories.py"
    assert baseline.is_file(), "冻结基线迁移 0043 消失——守卫需重新校准。"
    src = _strip_comment_lines(baseline.read_text(encoding="utf-8"))
    count = sum(len(p.findall(src)) for p in FORCED_ENVELOPE_PATTERNS)
    assert count >= 4, (
        f"0043 中强制信封断言只匹配到 {count} 处（应 ≥4：两个约束各含 "
        "`? 'K_enclave'` 与 `length(...->>'K_enclave')`）——pattern 可能已失效，"
        "守卫存在空转风险，请检查 0043 是否重构或 pattern 是否需更新。"
    )


def test_guard_detects_every_escape_form(tmp_path):
    """自测：4 种等价写法**逐个**都必须被抓到。

    原守卫只认 `? 'K_enclave'` 一种字面量，另外三种可以大摇大摆绕过——
    这个测试就是防止守卫退化回那个状态。用 tmp_path 隔离，不污染真的
    alembic 目录（避免被 conftest 的迁移一致性检查在收集阶段拦截）。
    """
    forms = {
        "9001_exists_op.py": "CONSTRAINT ck_a CHECK (doc ? 'K_enclave')",
        "9002_length.py": "CONSTRAINT ck_b CHECK (length(doc->>'K_enclave') > 0)",
        "9003_not_null.py": "CONSTRAINT ck_c CHECK (doc->>'K_enclave' IS NOT NULL)",
        "9004_jsonb_exists.py": "CONSTRAINT ck_d CHECK (jsonb_exists(doc,'K_enclave'))",
    }
    missed = []
    for name, sql in forms.items():
        d = tmp_path / name[:4]
        d.mkdir()
        (d / name).write_text(f'SQL = """\nCREATE TABLE t (\n  {sql}\n);\n"""\n',
                              encoding="utf-8")
        if not _scan_migrations(version_dirs=(d,), allowlist=set()):
            missed.append(f"{name}: {sql}")
    assert not missed, (
        "以下强制 K_enclave 等价写法能绕过守卫（守卫已退化，等于没堵）：\n"
        + "\n".join(f"  {m}" for m in missed)
    )


def test_guard_ignores_documentation_mentions(tmp_path):
    """整行注释里提到 K_enclave 不算违规——否则解释「本表不带信封 CHECK」的
    迁移（如 alembic_tee/0004_full_table_alignment）会被误判成违规。"""
    d = tmp_path / "docs_only"
    d.mkdir()
    (d / "9005_comment_only.py").write_text(
        "# 本表在 TEE 侧存明文，故不带 RDS 那边的 doc ? 'K_enclave' 约束\n"
        "SQL = \"\"\"\nCREATE TABLE t (doc JSONB NOT NULL);\n\"\"\"\n",
        encoding="utf-8")
    assert not _scan_migrations(version_dirs=(d,), allowlist=set()), (
        "整行注释中的文档性提及被误判为强制约束——会逼着后来者删注释来过 CI。"
    )


# ---------------------------------------------------------- 面 2：写入点冻结

def test_no_new_forced_envelope_write_sites():
    """底层强制封装 primitive 调用点只许减不许增。"""
    current = _scan_write_sites()
    grown = {
        path: (WRITE_SITE_BASELINE.get(path, 0), n)
        for path, n in current.items()
        if n > WRITE_SITE_BASELINE.get(path, 0)
    }
    assert not grown, (
        "检测到新增的『绕过 shape router 的底层信封封装点』，与 v6 默认明文冲突"
        "（这些点会无条件产生双收件人信封，明文档用户拿不到明文）：\n"
        + "\n".join(f"  {p}: 基线 {old} → 现在 {new}"
                    for p, (old, new) in sorted(grown.items()))
        + "\n\n正确做法：新写入走 _build_shared_envelope_for_store，由它按用户 "
        "content_encryption 偏好路由。\n若确属加密档专用路径，"
        "先在计划 Task 2.2 登记，再更新本测试的 WRITE_SITE_BASELINE。"
    )


def test_write_site_baseline_not_stale():
    """基线里的文件必须都还在——文件被改名/删除后基线会悄悄失效，
    新写入点就能在「基线里没这个 key」的掩护下溜进来（get(path, 0) 兜底）。"""
    root = BACKEND.parent
    missing = [p for p in WRITE_SITE_BASELINE if not (root / p).is_file()]
    assert not missing, (
        "写入点基线引用了不存在的文件（重命名或删除后未同步基线）：\n"
        + "\n".join(f"  {p}" for p in missing)
        + "\n请重新核算 WRITE_SITE_BASELINE，否则守卫对这些路径已失效。"
    )
