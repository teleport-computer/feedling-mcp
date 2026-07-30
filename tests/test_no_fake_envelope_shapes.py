"""守卫：伪造的信封形状不能再静默通过。

## 背景：同一类问题一天内抓到两次

- Task 1.1：`test_model_api_models_route.py` 用 `{"ciphertext": "x"}` 造信封。
  生产里没有 `ciphertext` 这个字段，`..._decrypt_failure_is_400` 因此从来没真正
  走到过 enclave——它验的是「一个不存在的形状会失败」。
- Task 2.3：`{"ct": ...}`（27 处）与 `{"envelope": {"id": ...}}`（16 处）同病。

老代码为什么容忍：写侧把整个 dict 原样甩给被 monkeypatch 的解密函数，**从不检查
形状**。于是这些测试一直"绿"着，却从没验证过真实行形状；等读写两侧真按形状路由，
43 个测试同时变红。

## 这里为什么不是「扫测试里的信封字典」

第一版守卫就是那么写的，随后自查发现：**两个真实反例都不带 `K_user`/`K_enclave`
等特征字段，扫描判据抓不到它们**——而它误报了 14 处合法代码（AEAD 的 `aad=`、
R2 指针行的 `env_meta`，都是名正言顺的「信封减 body」）。一个漏掉自己要防的事故、
却逼后人写豁免的守卫，比没有守卫更坏，已弃。

真正拦住这类事故的是**生产 helper 的严格性**：形状不认识就抛
`envelope_shape_unrecognized`。43 个假信封正是被它一次性照出来的。所以守卫要盯的
是「这份严格性不许被人为了让测试变绿而放松」。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from core import envelope as core_envelope  # noqa: E402

TESTS_DIR = pathlib.Path(__file__).resolve().parent

# 生产里可能出现的三种正文承载方式。任何 helper 都必须只认这三种。
#   body_ct  —— 双收件人信封
#   body     —— 明文行
#   body_key —— R2 指针行（正文在对象存储，行内两者皆无）
_REAL_SHAPE_KEYS = frozenset({"body_ct", "body", "body_key"})

_UNRECOGNIZED = "envelope_shape_unrecognized"


@pytest.mark.parametrize("bogus", [
    {"ciphertext": "x"},                                  # Task 1.1 的真实反例
    {"v": 1, "id": "x", "ct": "deadbeef"},                # Task 2.3 的真实反例
    {"id": "env_a1"},                                     # Task 2.3 的另一个真实反例
    {"K_user": "a", "nonce": "b"},                        # 像信封但没有正文
    {},
])
def test_readers_reject_shapes_that_cannot_exist(bogus):
    """读侧 helper 必须拒绝不认识的形状，而不是当成空内容读过去。

    放松这一条，上面那三个真实反例又会静默通过，而假信封写的断言验不到真实路径。
    """
    with pytest.raises(ValueError, match=_UNRECOGNIZED):
        core_envelope.read_envelope_body(bogus, "ak", purpose="guard")


@pytest.mark.parametrize("bogus", [
    {"ciphertext": "x"},
    {"v": 1, "id": "x", "ct": "deadbeef"},
    {"id": "env_a1"},
    {},
])
def test_storage_field_extraction_rejects_the_same_shapes(bogus):
    """写侧同理：拒绝，而不是落一行没有正文的空壳。"""
    with pytest.raises(ValueError, match=_UNRECOGNIZED):
        core_envelope.envelope_storage_fields(bogus)


def test_upload_gate_reports_missing_shape_instead_of_accepting():
    """上传闸对不认识的形状必须报缺失（按信封口径），不能放行。"""
    err = core_envelope.validate_uploaded_envelope(
        {"ciphertext": "x", "visibility": "shared", "owner_user_id": "u"},
        user_id="u")

    assert err is not None
    assert err["error"] == "envelope_missing_fields"
    assert "body_ct" in err["detail"]


def test_helpers_agree_on_which_keys_carry_a_body():
    """三种承载方式之外不许再冒出第四种判别键。

    形状判别散落在读侧、写侧、落库、swap 四处，靠的都是同一组键名。有人新增一种
    承载方式却只改其中一两处，就会出现「写得进去读不出来」——而且是静默的。
    """
    source = pathlib.Path(core_envelope.__file__).read_text()
    tree = ast.parse(source)

    referenced = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.startswith("body")
    }

    unexpected = referenced - _REAL_SHAPE_KEYS - {"body_ct_len"}
    assert not unexpected, (
        f"core/envelope.py 里出现了新的 body* 键 {sorted(unexpected)}——"
        "新增正文承载方式要同时更新读侧/写侧/落库/swap 四处判别，"
        "并把它加进本守卫的 _REAL_SHAPE_KEYS。")


def test_sealed_fields_stay_in_sync_with_the_shape_clearer():
    """`replace_record_shape` 清理的密文字段必须覆盖 `_SEALED_FIELDS` 全集。

    漏掉一个就意味着信封转明文后行上仍留着密文残留，而读侧 `body_ct` 优先——
    用户以为已转明文，读到的还是换之前的旧内容，且不报错。
    """
    record = {key: "residue" for key in core_envelope._SEALED_FIELDS}
    record["body_ct"] = "Y3Q="

    core_envelope.replace_record_shape(
        record, {"body": "new", "owner_user_id": "u", "visibility": "shared"})

    leftover = set(record) & set(core_envelope._SEALED_FIELDS)
    assert not leftover, f"信封转明文后仍残留密文字段：{sorted(leftover)}"
