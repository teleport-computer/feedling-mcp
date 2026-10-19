"""Causal slot-idle proof for an explicitly reviewed exclusive test parent.

No global state changes. This deliberately cannot certify arbitrary shared
runners, missing pools, legacy heartbeats, or process restarts.
"""
import math

POOLS = ('foreground', 'wake', 'heavy')


def rows(metrics, scope):
    all_rows = metrics.get('worker_heartbeats')
    if not isinstance(all_rows, list) or metrics.get('worker_heartbeat_count') != len(all_rows):
        raise RuntimeError('heartbeat inventory incomplete')
    expected = set(scope['worker_ids'].values())
    if any(r.get('kind') == 'turn' and r.get('worker_id') not in expected for r in all_rows):
        raise RuntimeError('unreviewed turn parent in exclusive runner inventory')
    selected = {}
    for pool in POOLS:
        found = [r for r in all_rows if r.get('worker_id') == scope['worker_ids'][pool]]
        if len(found) != 1:
            raise RuntimeError('drain parent missing or duplicate')
        row = found[0]
        state = row.get('runtime_state', {})
        aggregate = state.get('slots', {})
        report = state.get('drain', {})
        if (row.get('kind') != 'turn' or row.get('pool') != pool
                or not finite(row.get('age_sec')) or not 0 <= row['age_sec'] <= 30
                or not finite(row.get('beat_at_epoch'))
                or aggregate.get('configured') != scope['pool_slots'][pool]
                or aggregate.get('healthy') != aggregate.get('configured')
                or aggregate.get('restarting') != 0
                or not isinstance(report.get('parent_generation'), str)
                or report['parent_generation'] != scope['parent_generation']):
            raise RuntimeError('drain parent unknown/unhealthy')
        slots = report.get('slots')
        if not isinstance(slots, list) or len(slots) != scope['pool_slots'][pool]:
            raise RuntimeError('drain slots incomplete')
        ids = set()
        for slot in slots:
            sid = slot.get('slot_id')
            if (not isinstance(sid,str) or not sid or sid in ids
                    or not isinstance(slot.get('slot_generation'),str) or not slot['slot_generation']
                    or type(slot.get('pid')) is not int or slot['pid'] <= 0
                    or not finite(slot.get('sample_monotonic'))
                    or not finite(slot.get('progress_monotonic'))
                    or slot['progress_monotonic'] > slot['sample_monotonic']
                    or slot.get('state') not in ('idle','busy')):
                raise RuntimeError('drain slot unknown')
            ids.add(sid)
        selected[pool] = row
    if len({r['runtime_state']['drain']['parent_generation'] for r in selected.values()}) != 1:
        raise RuntimeError('drain pools belong to different parents')
    return selected


def finite(v):
    return type(v) in (float,int) and math.isfinite(v) and v >= 0


def identity(row):
    report = row['runtime_state']['drain']
    return (report['parent_generation'], sorted((s['slot_id'],s['slot_generation'],s['pid']) for s in report['slots']))


def checked_projection(row):
    """Copy only causal fields from a row already accepted by rows()."""
    report = row['runtime_state']['drain']
    return {
        'worker_id': row['worker_id'], 'pool': row['pool'],
        'beat_at_epoch': row['beat_at_epoch'], 'age_sec': row['age_sec'],
        'parent_generation': report['parent_generation'],
        'slots': [{k: slot[k] for k in (
            'slot_id', 'slot_generation', 'pid', 'sample_monotonic',
            'progress_monotonic', 'state')} for slot in report['slots']],
    }


class Proof:
    def __init__(self, initial, scope, fence_epoch):
        self.scope, self.fence_epoch = scope, fence_epoch
        self.initial = rows(initial, scope)
        self.after_beat = {}
        self.barriers = {}
        # Constant stage count: never retain every poll or arbitrary metrics.
        self.evidence = {'fence_epoch': fence_epoch, 'pools': {
            pool: {'initial': checked_projection(row)}
            for pool, row in self.initial.items()}}

    def observe(self, metrics):
        current = rows(metrics, self.scope)
        ready = True
        for pool, row in current.items():
            if identity(row) != identity(self.initial[pool]):
                raise RuntimeError('drain parent/slot restarted')
            beat = row['beat_at_epoch']
            slots = row['runtime_state']['drain']['slots']
            if pool not in self.after_beat:
                if beat > self.fence_epoch:
                    self.after_beat[pool] = beat
                    self.evidence['pools'][pool]['after_beat'] = checked_projection(row)
                ready = False
            elif pool not in self.barriers:
                # This sample belongs to a later iteration of the SAME awaited
                # heartbeat loop: its collection follows the prior DB write,
                # which itself followed the fence. No wall-clock comparison
                # between DB, parent, and child is required.
                if beat > self.after_beat[pool]:
                    self.barriers[pool] = {s['slot_id']:s['sample_monotonic'] for s in slots}
                    self.evidence['pools'][pool]['barrier'] = checked_projection(row)
                ready = False
            elif any(s['state'] != 'idle' or s['progress_monotonic'] <= self.barriers[pool][s['slot_id']]
                     for s in slots):
                ready = False
        if ready:
            for pool, row in current.items():
                self.evidence['pools'][pool]['final'] = checked_projection(row)
        return current if ready else None
