"""Fixed GPT-4o-mini OpenRouter text lane; context reserve is not token counting.

No configurable provider/model, count endpoint, fallback or hosted tool. Review
of context/fee/error semantics remains necessary before issuing a run receipt.
"""
import hashlib
import json
import math
from pathlib import Path
import time

ENDPOINT = 'https://openrouter.ai/api/v1/chat/completions'
MODEL = 'openai/gpt-4o-mini'
POLICY = {'only':['openai'], 'allow_fallbacks':False, 'require_parameters':True,
          'max_price':{'prompt':0.15, 'completion':0.60, 'request':0}}


def payload(value, limits):
    from .bounded_proxy import Refused
    allowed = {'model','messages','tools','tool_choice','max_tokens','stream',
               'temperature','top_p','stop','response_format','parallel_tool_calls'}
    if not isinstance(value,dict) or set(value)-allowed or value.get('model') != MODEL:
        raise Refused('or_shape')
    if value.get('stream',False) is not False:
        raise Refused('or_stream')
    messages = value.get('messages')
    if not isinstance(messages,list) or not messages:
        raise Refused('or_messages')
    for item in messages:
        if (not isinstance(item,dict) or set(item)-{'role','content','tool_calls','tool_call_id','name'}
                or item.get('role') not in {'system','user','assistant','tool'}):
            raise Refused('or_message')
        # No URLs/images/audio/attachments or server-side conversation reference.
        if not isinstance(item.get('content'),str):
            raise Refused('or_text_only')
        calls=item.get('tool_calls',[])
        if not isinstance(calls,list):
            raise Refused('or_tool_history')
        for call in calls:
            if (not isinstance(call,dict) or set(call)-{'id','type','function'}
                    or call.get('type') != 'function' or not isinstance(call.get('function'),dict)
                    or set(call['function'])-{'name','arguments'}
                    or not all(isinstance(call['function'].get(k),str) for k in ('name','arguments'))):
                raise Refused('or_tool_history')
    tools=value.get('tools',[])
    if not isinstance(tools,list) or any(not isinstance(t,dict) or set(t)-{'type','function'}
            or t.get('type')!='function' or not isinstance(t.get('function'),dict) for t in tools):
        raise Refused('or_no_hosted_tools')
    cap=value.get('max_tokens')
    if type(cap) is not int or cap <= 0:
        raise Refused('or_output_limit')
    result=dict(value, provider=POLICY, tool_choice='none', stream=False,
                max_tokens=min(cap,limits['output_tokens_per_start'],1024), n=1)
    raw=json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>limits['request_bytes']:
        raise Refused('or_input_bytes')
    return raw


def verify(manifest, *, now=None):
    """Authenticate no facts here: bind independently reviewed facts and expiry.

    No enabling receipt is shipped with this implementation. Evidence hashes
    alone are not proof that an account allocation or billing assertion is true.
    """
    def read_bound(path, expected):
        p=Path(path)
        if not p.is_absolute() or p.is_symlink() or not p.is_file():
            raise RuntimeError('or_evidence_missing')
        raw=p.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected:
            raise RuntimeError('or_evidence_changed')
        return raw
    from .contract import admission_policy
    policy = admission_policy(manifest)
    hashes=manifest['receipt_sha256']
    value=json.loads(read_bound(manifest['input_bound_receipt'],hashes['input_bound_receipt']))
    expected={'run_id':manifest['run_id'],'model':MODEL,'endpoint':ENDPOINT,
              'account_scope':manifest['billing']['account_scope'],
              'billing':manifest['billing'],'limits':manifest['limits'],
              'provider_policy':POLICY,'admission_policy':policy,'context_tokens':128000,
              'context_is_full_billable_input_upper_bound':True,
              'output_limit_includes_all_billable_tokens':True,
              'no_extra_fees_or_byok':True,'errors_do_not_exceed_reserved_cost':True,
              'authorized':True,
              'receipt_sha256':{k:v for k,v in hashes.items() if k!='input_bound_receipt'}}
    if 'execution_window' in manifest:
        expected['execution_window'] = manifest['execution_window']
    if any(value.get(k)!=v for k,v in expected.items()) or admission_policy(
            {'lane': manifest['lane'], 'admission_policy': value.get('admission_policy')}) != policy:
        raise RuntimeError('or_evidence_binding')
    for name,h in expected['receipt_sha256'].items():
        read_bound(manifest[name],h)
    for name in ('context_evidence','pricing_evidence','funding_evidence','authorization_evidence'):
        e=value.get(name)
        if not isinstance(e,dict):raise RuntimeError('or_evidence_missing')
        read_bound(e.get('path',''),e.get('sha256'))
    # Last check AFTER all filesystem reads; durable reservation calls this again.
    at=time.time() if now is None else now
    start,end=value.get('valid_from_epoch'),value.get('valid_until_epoch')
    if (type(start) not in (int,float) or type(end) not in (int,float)
            or not math.isfinite(start) or not math.isfinite(end) or not start<=at<end):
        raise RuntimeError('or_authorization_expired')
    return value
