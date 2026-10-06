"""T617: real sanitizer / pi stream -> failure -> bounded trace, no provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

for key, value in {
    'FEEDLING_API_URL': 'http://localhost:5001',
    'FEEDLING_API_KEY': 'test_key_00000000', 'AGENT_MODE': 'http',
    'AGENT_HTTP_URL': 'http://localhost:8080/chat',
    'CHECKPOINT_FILE': '/tmp/feedling_test_checkpoint.json',
}.items():
    os.environ.setdefault(key, value)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import tools.chat_resident_consumer as c
from agent_protocol_core import self_thinking as st
from debug_trace import _safe_detail
from admin import data_track
from notices import error_contract


@pytest.fixture
def traces(monkeypatch):
    events = []
    monkeypatch.setattr(c, '_emit_debug_trace', lambda _subsystem, kind, **kw: events.append({'type': kind, **kw}))
    monkeypatch.setattr(c, '_report_runtime_error', lambda *a, **kw: None)
    monkeypatch.setattr(c, '_emit_recall_completed', lambda **kw: None)
    monkeypatch.setattr(c, '_preserve_reply_parse_failure', lambda *a, **kw: None)
    monkeypatch.setenv('FEEDLING_THINK_GATE', '1')
    return events


def failure_detail(exc, events):
    c._notify_agent_turn_failure(exc, foreground=False, lane='chat', trace_id='t617')
    detail = events[-1]['detail']
    assert events[-1]['type'] == 'agent.turn.failure'
    assert len(detail) <= 20
    assert _safe_detail(detail) == detail
    public = data_track._debug_event_public_json({'type': 'agent.turn.failure', 'detail': _safe_detail(detail)}, trace_public_fields={})['detail']
    for key in ('sanitizer_reason', 'provider_status_class', 'provider_status_code', 'raw_reply_head', 'raw_reply_tail', 'raw_reply_len', 'think_open_count', 'think_close_count'):
        if key in detail:
            assert public[key] == detail[key]
    return detail


class Response:
    status_code = 200
    text = ''
    headers = {}

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_closed_reason_and_status_vocabularies():
    assert c.SANITIZER_REASONS == {
        'thinking_gate_failed', 'thinking_gate_salvaged', 'protocol_leak', 'file_citation', 'unknown',
    }
    assert c.PROVIDER_STATUS_CLASSES == {'4xx', '5xx', 'none'}
    assert c.SANITIZER_REASONS is error_contract.RESIDENT_SANITIZER_REASONS
    assert c.PROVIDER_STATUS_CLASSES is error_contract.PROVIDER_STATUS_CLASSES


@pytest.mark.parametrize('wire', ['simple', 'openai'])
@pytest.mark.parametrize('raw,reason', [
    # Thinking-only text whose tags the strict gate refuses and the salvage
    # layer cannot rescue either (T656): nothing outside the tags to deliver.
    ('  <think>' + 'x' * 340 + '<aside>nested</think>  ', 'thinking_gate_failed'),
    ('{"actions": [', 'protocol_leak'),
    ('The user wrote "sweet!" ...\nI think it is best to respond ...', 'unknown'),
])
def test_http_sanitizer_failure_real_handoff(monkeypatch, traces, wire, raw, reason):
    body = {'reply': raw} if wire == 'simple' else {'choices': [{'message': {'role': 'assistant', 'content': raw}}]}
    monkeypatch.setattr(c._HTTP, 'post', lambda *a, **kw: Response(body))
    with pytest.raises(ValueError, match=c.SANITIZED_TO_EMPTY_MARK) as caught:
        getattr(c, '_call_agent_http_' + wire)('hi')
    notice = c.classify_agent_error(caught.value)
    assert notice.error_class == 'reply_parse_failed'
    assert notice.blame == 'system'
    assert notice == c.classify_agent_error(ValueError(str(caught.value)))
    assert notice.detail == str(caught.value)[:200]
    assert c._system_notice_body(notice) == f'⚠️ {notice.user_text}\n详情: {str(caught.value)[:200]}'
    assert raw not in notice.detail
    detail = failure_detail(caught.value, traces)
    assert detail['sanitizer_reason'] == reason
    assert detail['raw_reply_head'] == raw[:300]
    assert detail['raw_reply_tail'] == raw[-120:]
    assert detail['raw_reply_len'] == len(raw)
    assert detail['think_open_count'] == (2 if reason == 'thinking_gate_failed' else 0)
    assert detail['think_close_count'] == (1 if reason == 'thinking_gate_failed' else 0)


@pytest.mark.parametrize('wire', ['simple', 'openai'])
def test_http_nested_think_with_reply_text_is_salvaged_not_failed(monkeypatch, traces, wire):
    """T656: the same nested shape WITH text after the tags used to be a
    reply_parse_failed turn; now the text is delivered and the thinking dropped.
    The strict gate still refuses it — the salvage layer is what changed."""
    raw = '  <think>' + 'x' * 340 + '<aside>nested</think> tail  '
    from agent_protocol_core import self_thinking as st
    assert st.strip_all_thinking(raw, sanitize=False)[0] == st.FAILED
    body = {'reply': raw} if wire == 'simple' else {'choices': [{'message': {'role': 'assistant', 'content': raw}}]}
    monkeypatch.setattr(c._HTTP, 'post', lambda *a, **kw: Response(body))
    out = getattr(c, '_call_agent_http_' + wire)('hi')
    text = out if isinstance(out, str) else '\n'.join(out.get('messages') or [])
    assert 'tail' in text
    assert 'nested' not in text and 'xxx' not in text and '<' not in text


@pytest.mark.parametrize('fallback', [True, False])
def test_call_agent_carries_local_gate_reason_through_fallback_or_raise(monkeypatch, traces, fallback):
    raw = '<aside>unfinished'
    monkeypatch.setattr(c, 'AGENT_MODE', 'http')
    monkeypatch.setattr(c, 'SEND_FALLBACK_ON_AGENT_ERROR', fallback)
    monkeypatch.setattr(c, 'call_agent_http', lambda *a, **kw: raw)
    monkeypatch.setattr(c, '_call_with_resident_busy_poll', lambda fn, **kw: fn())
    if fallback:
        assert c.call_agent('hi', lane='chat') == [c.FALLBACK_REPLY]
        code = c._consume_reply_parse_failed()
        assert code == 'reply_parse_failed'
        assert c._consume_reply_parse_failed() == ''
        exc = c._reply_parse_failure_exc(code)
    else:
        with pytest.raises(ValueError) as caught:
            c.call_agent('hi', lane='chat')
        exc = caught.value
    detail = failure_detail(exc, traces)
    assert detail['sanitizer_reason'] == 'thinking_gate_failed'
    assert detail['raw_reply_head'] == raw
    # A subsequent successful call must not inherit the prior observation.
    monkeypatch.setattr(c, 'call_agent_http', lambda *a, **kw: 'hello')
    assert c.call_agent('hi', lane='chat')['messages'] == ['hello']
    assert c._consume_reply_parse_failed() == ''


def test_protocol_suppressor_records_its_own_drop(traces):
    raw = '"actions":[{"type":"send_message","text":"leak"}]}'
    turn = c.AgentTurn(messages=[raw], thinking_summary='{"messages": [')
    c._suppress_torn_protocol_leaks(turn, lane='proactive')
    assert turn.messages == [] and turn.thinking_summary == ''
    detail = failure_detail(c._reply_parse_failure_exc(c._ReplyParseFailureCode('reply_parse_failed', turn)), traces)
    assert detail['sanitizer_reason'] == 'protocol_leak'
    assert detail['raw_reply_head'] == raw


def test_file_citation_records_only_actual_emptying(traces):
    raw = ':codex-file-citation{path="/private/tmp/report.pdf" purpose="output"}'
    turn = c.AgentTurn(messages=[raw])
    cleaned, removed = c._sanitize_outbound_file_reply(raw, diagnostics=turn)
    assert removed and not cleaned.strip()
    assert turn.sanitizer_reason == 'file_citation'
    detail = failure_detail(c._sanitized_reply_error('no usable reply', turn), traces)
    assert detail['sanitizer_reason'] == 'file_citation'
    assert detail['raw_reply_head'] == raw
    unchanged = c.AgentTurn()
    assert c._sanitize_outbound_file_reply('hello', diagnostics=unchanged) == ('hello', False)
    assert unchanged.sanitizer_reason == ''


def test_unknown_and_provider_forged_diagnostics(traces):
    turn = c._agent_turn_from_raw({'reply': '<think>broken', 'sanitizer_reason': 'file_citation',
                                 'raw_reply_diagnostics': {'raw_reply_head': 'FORGED'}})
    assert turn.sanitizer_reason == 'thinking_gate_failed'
    assert turn.raw_reply_diagnostics['raw_reply_head'] == '<think>broken'
    exc = ValueError(c.SANITIZED_TO_EMPTY_MARK)
    detail = failure_detail(exc, traces)
    assert detail['sanitizer_reason'] == 'unknown'
    assert detail['raw_reply_len'] is None
    exc.sanitizer_reason = 'PRIVATE INVALID VALUE'
    assert failure_detail(exc, traces)['sanitizer_reason'] == 'unknown'


@pytest.mark.parametrize('tag', st._TAG_WORDS)
def test_tag_counts_derive_from_shared_vocabulary(tag):
    raw = f'<{tag}><ns:{tag.upper()}>x</{tag}> <{tag}'
    d = c._raw_reply_diagnostics(raw)
    assert d['think_open_count'] == 2 and d['think_close_count'] == 1
    assert d['raw_reply_len'] == len(raw)


@pytest.mark.parametrize('error,code,klass', [
    ('503 upstream unavailable', 503, 'upstream_unavailable'),
    ('provider_http_403: Forbidden', 403, 'auth_invalid'),
    ('402 insufficient credits', 402, 'quota_insufficient'),
    ('no content', None, 'provider_error_unclassified'),
])
def test_pi_jsonl_no_reply_to_both_real_trace_sinks(monkeypatch, traces, error, code, klass):
    raw = json.dumps({'type': 'message_end', 'message': {
        'role': 'assistant', 'stopReason': 'error', 'errorMessage': error, 'content': [],
    }})
    assert c._pi_turn_from_stream(raw) == ('', '')
    monkeypatch.setattr(c, 'AGENT_CLI_CMD', 'pi --mode json')
    monkeypatch.setattr(c, '_prepare_cli_command', lambda *a, **kw: (['pi', '--mode', 'json'], None))
    monkeypatch.setattr(c, '_run_cli_subprocess', lambda *a, **kw: subprocess.CompletedProcess(['pi'], 0, raw, ''))
    monkeypatch.setattr(c, 'AGENT_RUNTIME_METADATA', {'provider': 'openai_compatible', 'model': 'relay-model'})
    with pytest.raises(RuntimeError, match='pi agent produced no reply') as caught:
        c.call_agent_cli('hi', lane='chat', trace_id='t617')
    notice = c.classify_agent_error(caught.value)
    assert notice.error_class == klass
    # Diagnostics must not alter the existing classification/blame/text.
    assert notice == c.classify_agent_error(RuntimeError(str(caught.value)))
    assert notice.detail == str(caught.value)[:200]
    assert c._system_notice_body(notice) == f'⚠️ {notice.user_text}\n详情: {str(caught.value)[:200]}'
    error_event = next(e for e in traces if e['type'] == 'agent.model.call.error')
    assert not any(e['type'] == 'agent.model.call.done' for e in traces)
    expected_class = f'{code // 100}xx' if code else 'none'
    for detail in [error_event['detail'], failure_detail(caught.value, traces)]:
        assert detail['provider_status_code'] == code
        assert detail['provider_status_class'] == expected_class
        assert 'raw_reply_head' not in detail
        assert len(detail) <= 20
        bounded = _safe_detail(detail)
        assert bounded['provider_status_code'] == code
        assert bounded['provider_status_class'] == expected_class


@pytest.mark.parametrize('detail,code', [
    ('HTTP/1.1 502 Bad Gateway', 502), ('Error: 429 rate limit', 429),
    ('unexpected status: 403 Forbidden', 403), ('api_status=401', 401),
    ('generated 503 tokens', None), ('request id 503', None), ('5030', None),
    ('HTTP 200 empty', None), ('HTTP 399 redirect', None),
])
def test_status_extraction_requires_status_shape(detail, code):
    exc = c._pi_no_reply_error(detail)
    assert exc.provider_status_code == code
    assert exc.provider_status_class in c.PROVIDER_STATUS_CLASSES


def test_safe_detail_head_exception_is_narrow():
    detail = {'error_class': 'reply_parse_failed', 'raw_reply_head': 'x' * 500, 'other': 'y' * 500}
    result = _safe_detail(detail)
    assert len(result['raw_reply_head']) == 300 and len(result['other']) == 200
    detail['error_class'] = 'provider_empty_reply'
    assert len(_safe_detail(detail)['raw_reply_head']) == 200


def test_admin_excerpt_projection_is_failure_scoped():
    for event_type, error_class in [('agent.turn.failure', 'provider_empty_reply'), ('agent.reply', 'reply_parse_failed')]:
        projected = data_track._debug_event_public_json({'type': event_type, 'detail': {
            'error_class': error_class, 'raw_reply_head': 'PRIVATE HEAD',
            'sanitizer_reason': 'INVALID CONTENT', 'provider_status_class': '503 SECRET',
        }}, trace_public_fields={})['detail']
        assert projected['raw_reply_head'] != 'PRIVATE HEAD'
        assert projected['sanitizer_reason'] != 'INVALID CONTENT'
        assert projected['provider_status_class'] != '503 SECRET'
