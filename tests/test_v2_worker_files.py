"""V2 file-text injection into the prompt tail (worker._inject_tail_files).

Mirrors test_v2_worker_images.py. Lets a tool-less HTTP model read an uploaded
artifact by replacing the `[file: name]` marker row with a cached encrypted VFS
text view. Cache misses are materialized and parsed by a lazy sandbox provider,
never by the backend process.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import worker


def _file_row(mid, name="doc.pdf"):
    return {"id": mid, "ts": 1.0, "role": "user", "content": f"[file: {name}]",
            "has_file": True, "file_name": name,
            "file_mime": "application/pdf"}


def _fake_reader(payload):
    def _read(user_id, message_ids):
        return {mid: payload[mid] for mid in message_ids if mid in payload}
    return _read


def test_inject_replaces_marker_with_name_and_extracted_text():
    tail = [_file_row("m1", name="账单.pdf")]
    out = worker._inject_tail_files(
        tail, user_id="u",
        read_files=_fake_reader({"m1": {"file_name": "账单.pdf", "file_mime": "application/pdf",
                                        "text": "日期\t金额\n07-01\t12.50", "truncated": False}}))
    assert out[0]["content"] == "[file: 账单.pdf]\n日期\t金额\n07-01\t12.50"


def test_inject_appends_truncation_note_when_truncated():
    tail = [_file_row("m1", name="big.xlsx")]
    out = worker._inject_tail_files(
        tail, user_id="u",
        read_files=_fake_reader({"m1": {"file_name": "big.xlsx", "text": "row", "truncated": True}}))
    assert out[0]["content"] == "[file: big.xlsx]\nrow\n（文件内容较长，已截断）"


def test_inject_keeps_marker_when_extraction_empty():
    """scanned pdf / unsupported binary -> reader returns nothing -> row unchanged."""
    tail = [_file_row("m1", name="scan.pdf")]
    out = worker._inject_tail_files(tail, user_id="u", read_files=_fake_reader({}))
    assert out[0]["content"] == "[file: scan.pdf]"


def test_inject_surfaces_fail_closed_sandbox_unavailable():
    tail = [_file_row("m1", name="contract.pdf")]
    out = worker._inject_tail_files(
        tail,
        user_id="u",
        read_files=_fake_reader({
            "m1": {"file_name": "contract.pdf", "error": "sandbox_unavailable"},
        }),
    )
    assert out[0]["content"] == (
        "[file: contract.pdf]\n[artifact unavailable: sandbox_unavailable]"
    )


def test_inject_only_takes_the_most_recent_N_files():
    tail = [_file_row(f"m{i}", name=f"f{i}.txt") for i in range(5)]
    payload = {f"m{i}": {"file_name": f"f{i}.txt", "text": f"body{i}", "truncated": False}
               for i in range(5)}
    out = worker._inject_tail_files(tail, user_id="u", read_files=_fake_reader(payload))
    injected = [i for i, r in enumerate(out) if r["content"].startswith("[file:") and "\n" in r["content"]]
    assert injected == [3, 4]                       # newest _TAIL_FILE_LIMIT=2
    assert out[0]["content"] == "[file: f0.txt]"     # older rows stay bare markers
    assert worker._TAIL_FILE_LIMIT == 2


def test_inject_degrades_silently_when_reader_raises():
    def _boom(user_id, message_ids):
        raise RuntimeError("enclave down")
    tail = [_file_row("m1")]
    out = worker._inject_tail_files(tail, user_id="u", read_files=_boom)
    assert out[0]["content"] == "[file: doc.pdf]"     # no-filler: never fail the turn


def test_inject_is_a_noop_without_a_reader_or_without_files():
    tail = [{"id": "m1", "ts": 1.0, "role": "user", "content": "hi"}]
    assert worker._inject_tail_files(tail, user_id="u", read_files=None) == tail
    assert worker._inject_tail_files(tail, user_id="u", read_files=_fake_reader({})) == tail


def test_inject_does_not_mutate_the_input_tail():
    tail = [_file_row("m1")]
    original = dict(tail[0])
    worker._inject_tail_files(
        tail, user_id="u",
        read_files=_fake_reader({"m1": {"file_name": "doc.pdf", "text": "body", "truncated": False}}))
    assert tail[0] == original
