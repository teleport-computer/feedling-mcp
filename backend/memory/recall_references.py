"""Explicitly required plaintext references, never optional auto-memory."""
from __future__ import annotations

import json
import time

import db
from core import plaintext_row
from memory import recall_contract, recall_select


def read(user_id, ids, deadline):
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return []
    if any(not isinstance(mid, str) or not mid for mid in wanted):
        raise recall_contract.RecallFailure('required_reference_invalid')
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise recall_contract.RecallFailure('required_reference_unavailable')
    try:
        with db.get_pool().connection(timeout=remaining) as conn:
            with conn.transaction():
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SELECT set_config('statement_timeout', %s, true)",
                             (str(max(1,int((deadline-time.monotonic())*1000))),))
                rows=conn.execute('SELECT moment_id,doc FROM memory_moments WHERE user_id=%s AND moment_id=ANY(%s)',
                                  (user_id,wanted)).fetchall()
        by_id={}
        for mid, row in rows:
            recall_contract.owner(row,user_id)
            if row.get('id') != mid or mid in by_id:
                raise recall_contract.RecallFailure('required_reference_binding_invalid',category='binding_error')
            by_id[mid]=row
        if set(by_id) != set(wanted):
            raise recall_contract.RecallFailure('required_reference_unavailable')
        cards=[]
        for mid in wanted:
            row=by_id[mid]
            if row.get('body_ct') or row.get('K_enclave') or row.get('visibility')=='local_only':
                raise recall_contract.RecallFailure('required_reference_unavailable')
            value=json.loads(plaintext_row.read_plaintext_row(row,user_id,ValueError).decode('utf-8'))
            if not isinstance(value,dict):
                raise ValueError('reference must be object')
            for field in ('summary','content','title','description'):
                if value.get(field) is not None and not isinstance(value[field],str):
                    raise ValueError('reference text invalid')
            card=recall_select.card_from_inner(value,row)
            if not any(str(card.get(key) or '').strip() for key in ('summary','content','title','description')):
                raise recall_contract.RecallFailure('required_reference_unavailable')
            cards.append(card)
        if time.monotonic()>deadline:
            raise recall_contract.RecallFailure('required_reference_unavailable')
        return cards
    except recall_contract.RecallFailure:
        raise
    except Exception as exc:
        raise recall_contract.RecallFailure('required_reference_unavailable') from exc
