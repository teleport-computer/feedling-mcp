"""Two fixed provider V2 text slices. Structural validation is not live approval.

OpenRouter compatibility: unversioned historical manifests/receipts remain
archival evidence and are rejected for execution. A newly reviewed package must
explicitly bind LEGACY_ADMISSION (original per-phase allocation enforcement) or
SHARED_ADMISSION in both manifest and input-bound receipt. Do not edit or reuse
an old claim/receipt to migrate it. The official OpenAI lane is unchanged.

Shared v2 counts every actual forward, including profile format corrections,
wrapper reissues and delivery-loss reissues, against the same six-start cap.
Only relay transport retries are zero; logical retries are not claimed zero.
Phases remain ordered observations, with no guaranteed dialogue allocation.
Observed errors stop all further admission; uncertain reservations never refund.
"""
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import re
from urllib.parse import urlsplit

RECEIPTS = ('funded_budget_receipt', 'model_availability_receipt', 'execution_approval_receipt', 'billing_contract_receipt', 'execution_environment_receipt', 'input_count_receipt')

OR_LANE = 'openrouter-v2-text'
OR_MODEL = 'openai/gpt-4o-mini'

# Explicit execution contracts. Historical unversioned OR artifacts are not
# executable with this tool; they must never be silently upgraded.
LEGACY_ADMISSION = {
    'version': 'or-phase-cap-v1', 'phase': 'enforced',
    'relay_transport_retries': 0,
    'product_reissues': 'count_all_actual_forwards_within_phase_and_total',
}
SHARED_ADMISSION = {
    'version': 'or-shared-cap-v2', 'phase': 'observational',
    'relay_transport_retries': 0,
    'product_reissues': 'count_all_actual_forwards_within_shared_total',
}

def admission_policy(value):
    policy = value.get('admission_policy')
    if value.get('lane') == OR_LANE:
        # JSON equality alone treats False as 0; require exact JSON types too.
        if not isinstance(policy, dict) or not any(
            digest(policy) == digest(p) for p in (LEGACY_ADMISSION, SHARED_ADMISSION)
        ):
            raise ValueError('REQUIRED_INPUT:admission_policy')
        return policy
    if 'admission_policy' in value:
        raise ValueError('REQUIRED_INPUT:admission_policy_lane')
    return None

def receipt_names(value):
    return RECEIPTS[:-1] + ('input_bound_receipt',) if value.get('lane') == OR_LANE else RECEIPTS

PHASES = ('setup', 'dialogue_1', 'dialogue_2', 'background')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def validate_manifest(value):
    def need(ok, name):
        if not ok:
            raise ValueError('REQUIRED_INPUT:' + name)
    need(isinstance(value, dict), 'manifest')
    value = json.loads(json.dumps(value))
    need(value.get('api_url') == 'https://test-api.feedling.app', 'test_origin')
    is_or = value.get('lane') == OR_LANE
    policy = admission_policy(value)
    shared = policy == SHARED_ADMISSION
    need(shared or 'execution_window' not in value, 'absolute_window_lane')
    need(value.get('lane') in ('openai-official-v2-text', OR_LANE), 'lane')
    need(value.get('model') == (OR_MODEL if is_or else 'gpt-5.4-mini'), 'fixed_model')
    need(isinstance(value.get('run_id'), str) and re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', value['run_id']), 'run_id')
    need(isinstance(value.get('expected_release'), str) and re.fullmatch('[0-9a-f]{40}', value['expected_release']), 'release')
    for name in receipt_names(value):
        need(isinstance(value.get(name), str) and value[name].strip(), name)
    need(isinstance(value.get('claim_path'), str) and value['claim_path'].startswith('/'), 'exclusive_claim_path')
    hashes = value.get('receipt_sha256')
    need(isinstance(hashes, dict), 'receipt_sha256')
    for name in receipt_names(value):
        need(isinstance(hashes.get(name), str) and re.fullmatch('[0-9a-f]{64}', hashes[name]), name+'_sha256')
    origin = value.get('relay_origin')
    need(isinstance(origin, str), 'relay_origin')
    u = urlsplit(origin)
    need(u.scheme == 'https' and u.hostname and not u.username and not u.password
         and u.path == '' and not u.query and not u.fragment and u.port in (None,443), 'relay_origin')
    need(type(value.get('relay_port')) is int and 1024 <= value['relay_port'] <= 65535, 'relay_port')
    scope = value.get('drain_scope')
    need(isinstance(scope,dict) and isinstance(scope.get('parent_generation'),str)
         and bool(scope['parent_generation']), 'reviewed_parent_generation')
    need(isinstance(scope,dict) and scope.get('exclusive_test_runner') is True
         and scope.get('config_action') == 'delete-model-api', 'reviewed_drain_scope')
    owners = scope.get('resident_owners')
    need(isinstance(owners,list) and owners and all(isinstance(x,str) and x for x in owners)
         and len(set(owners)) == len(owners), 'resident_guard_inventory')
    ids, counts = scope.get('worker_ids'), scope.get('pool_slots')
    need(isinstance(ids,dict) and isinstance(counts,dict), 'drain_inventory')
    for pool in ('foreground','wake','heavy'):
        need(isinstance(ids.get(pool),str) and ids[pool].endswith(':'+pool), 'drain_worker_id')
        need(type(counts.get(pool)) is int and counts[pool]>0, 'drain_pool_count')
    need(len({ids[p].rsplit(':',1)[0] for p in ids}) == 1, 'one_exclusive_parent')
    limits = value.get('limits')
    need(isinstance(limits, dict), 'limits')
    for name in ('starts', 'request_bytes', 'total_bytes', 'input_tokens_per_start', 'total_input_tokens', 'output_tokens_per_start',
                 'total_output_tokens', 'wall_seconds', 'drain_seconds', 'cleanup_seconds', 'poll_seconds', 'read_requests'):
        need(type(limits.get(name)) is int and limits[name] > 0, name)
    need(limits['output_tokens_per_start'] <= 1024, 'reasoning_and_visible_output_cap')
    if is_or:
        need(value.get('input_count') == {'enabled':False,'max_requests':0,'timeout_seconds':0,'usd_per_request_cap':'0'}, 'no_count_route')
    else:
        count = value.get('input_count')
        need(isinstance(count, dict), 'input_count')
        need(type(count.get('enabled')) is bool, 'input_count_enabled')
        need(type(count.get('max_requests')) is int and 0 < count['max_requests'] <= limits['starts'], 'count_requests')
        need(type(count.get('timeout_seconds')) is int and 0 < count['timeout_seconds'] <= 20, 'count_timeout')
        fee = count.get('usd_per_request_cap')
        need(isinstance(fee, str) and re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,9})?', fee), 'count_fee_cap')
    billing = value.get('billing')
    # Source-path minimum including cleanup, not a recommended execution cap.
    # Reviewed plans must additionally budget finite convergence/drain polling.
    need(limits['read_requests'] >= 48, 'minimum_complete_path_reads')
    need(isinstance(billing,dict), 'billing')
    need(billing.get('model') == value['model'] and billing.get('currency') == 'USD', 'exact_model_currency')
    need(billing.get('input_bound_kind') == ('fixed_provider_context' if is_or else 'responses_input_tokens'), 'verified_input_bound')
    need(type(billing.get('enforced_input_tokens_max')) is int
         and 0 < billing['enforced_input_tokens_max'] <= limits['input_tokens_per_start'], 'input_hard_cap_fits')
    need(billing.get('output_cap_includes_reasoning') is True, 'billable_output_bound')
    need(billing.get('exclusive_funding') is True and isinstance(billing.get('account_scope'),str)
         and billing['account_scope'], 'funding_scope')
    for field in ('uncached_input_usd_per_million','billable_output_usd_per_million',
                  'extra_usd_per_start','max_cost_usd','funded_remaining_usd','task_cap_usd'):
        amount = billing.get(field)
        need(isinstance(amount,str) and re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,9})?',amount), field)
        need(Decimal(amount) >= 0 if field == 'extra_usd_per_start' else Decimal(amount) > 0, field)
    need(limits['total_input_tokens'] >= billing['enforced_input_tokens_max'], 'input_reservations')
    need(cost_reservation(billing, limits['output_tokens_per_start']) + count_cost_reservation(value) <= cost_ceiling(billing), 'one_start_funded')
    if is_or:
        need(billing['enforced_input_tokens_max'] == 128000, 'full_context_reservation')
        need(billing['uncached_input_usd_per_million'] == '0.15' and billing['billable_output_usd_per_million'] == '0.60' and billing['extra_usd_per_start'] == '0', 'fixed_or_prices')
        need(limits['starts'] <= 6 and limits['wall_seconds'] <= 600, 'or_batch_cap')
        need(cost_ceiling(billing) <= 120000000, 'or_money_cap')
    if shared:
        from .deadlines import validate_window
        window = validate_window(value.get('execution_window'))
        need(window['cleanup_end_epoch'] - window['execution_end_epoch'] >= limits['cleanup_seconds'], 'absolute_cleanup_reserve')
        need('allocations' not in limits and 'phase_starts' not in limits, 'shared_no_phase_quotas')
        need(limits['starts'] == 6 and limits['input_tokens_per_start'] == 128000
             and limits['total_input_tokens'] == 768000
             and limits['output_tokens_per_start'] == 1024
             and limits['total_output_tokens'] == 6144, 'shared_fixed_caps')
        need(billing['max_cost_usd'] == '0.1188864'
             and cost_ceiling(billing) == 118886400, 'shared_funded_cap')
    else:
        allocations = limits.get('allocations')
        need(isinstance(allocations, dict) and set(allocations) == set(PHASES), 'allocations')
        phases = limits.get('phase_starts')
        need(isinstance(phases, dict) and set(phases) == set(PHASES), 'phase_starts')
        for name in PHASES:
            row = allocations[name]
            need(isinstance(row, dict) and set(row) == {'primary', 'retry', 'background'}, 'allocation_'+name)
            need(all(type(x) is int and x >= 0 for x in row.values()), 'allocation_numbers')
            need(sum(row.values()) == phases[name], 'allocation_sum')
            need(row['primary'] >= (0 if name == 'background' else 1), 'primary_start')
            need(type(phases[name]) is int and phases[name] >= (0 if name == 'background' else 1), name)
        # Each phase includes ALL actual starts while it is open, including retries,
        # continuations and background work. Classification never bypasses a cap.
        need(limits['starts'] == sum(phases.values()), 'phase_total')
    need(limits['total_output_tokens'] >= limits['starts'] * limits['output_tokens_per_start'], 'output_reservations')
    need(limits['total_bytes'] >= limits['request_bytes'], 'input_total')
    if not shared:
        limits['phase_starts'] = {name: phases[name] for name in PHASES}
    return value


def cost_ceiling(billing):
    """Integer nanodollars; minimum of proposal, eligible funding and task cap."""
    return int(min(Decimal(billing[k]) for k in
                   ('max_cost_usd','funded_remaining_usd','task_cap_usd')) * 1_000_000_000)


def cost_reservation(billing, output_tokens):
    # Reserve the configured ceiling only AFTER a full-payload official count
    # proves this request fits it. The ceiling alone is never evidence.
    value = (Decimal(billing['enforced_input_tokens_max']) * Decimal(billing['uncached_input_usd_per_million']) * 1000
             + Decimal(output_tokens) * Decimal(billing['billable_output_usd_per_million']) * 1000
             + Decimal(billing['extra_usd_per_start']) * 1_000_000_000)
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def count_cost_reservation(manifest):
    return int((Decimal(manifest['input_count']['usd_per_request_cap']) * 1_000_000_000).to_integral_value(rounding=ROUND_CEILING))
