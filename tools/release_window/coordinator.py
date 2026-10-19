"""Own terminal failure independently of a worker blocked in local I/O.

A timed-out worker may have already issued remote work. No cancellation,
fixture disappearance or refunded reservation is inferred from this exit.
"""
import copy
import queue
import threading
import time
import uuid

from tools.e2e.client import _write_private_json


class Progress(dict):
    def __init__(self, initial):
        super().__init__(initial)
        self.lock = threading.Lock()
    def __setitem__(self, key, value):
        with self.lock:
            super().__setitem__(key, value)
    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(self))


def coordinate(work, window, out, initial, progress, *, wall=time.time, clock=time.monotonic):
    """Wait through the bounded cleanup interval, never on the worker's lock.

    The worker alone performs cleanup after its synchronous execution has
    unwound and the existing relay/ownership/drain proof succeeds. No parallel
    cleanup is started while conflicting I/O is unresolved.
    """
    mailbox = queue.Queue()
    terminal = threading.Event()
    state = Progress(progress)
    result = copy.deepcopy(initial)
    end = clock() + max(0, window['cleanup_end_epoch']-wall())
    recovery = []
    sequence = [0]
    def snapshot(value):
        name = f'coordinator-recovery-{sequence[0]:04d}.json'
        sequence[0] += 1
        _write_private_json(out/name, value)
    def publish_recovery(value):
        value = copy.deepcopy(value)
        _write_private_json(out/('owned-recovery-'+uuid.uuid4().hex+'.json'), value)
        mailbox.put(('recovery', value))
    def worker():
        try:
            work(result, state, publish_recovery)
        except Exception as exc:
            result.update(status='INCOMPLETE', error_type=type(exc).__name__,
                          error_stage=state.snapshot().get('stage'))
        # Worker cannot write the terminal result. Only immutable recovery facts
        # may be appended after the coordinator has committed failure.
        completed = copy.deepcopy(result)
        # Always preserve completion facts, including the boundary race.
        _write_private_json(out/'late-worker-recovery.json', {
                'status':'RECOVERY_ONLY_NOT_ACCEPTANCE', 'observed_result':completed,
                'phase':state.snapshot(), 'at':wall()})
        mailbox.put(('completed', completed))
    snapshot({
        'phase':'execution', 'ownership':'UNCONFIRMED_UNTIL_RECOVERY_RECORD',
        'execution_window':window, 'claim_reusable':False,
        'uncertain_starts':'FULL_RESERVATION_RETAINED', 'recovery':[]})
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    phase = 'execution'
    while True:
        remaining = min(window['cleanup_end_epoch']-wall(), end-clock())
        if remaining <= 0:
            terminal.set()
            final = dict(initial, status='INCOMPLETE', cleanup='CLEANUP_INCOMPLETE',
                         io_status='IO_UNCONFIRMED', recovery_required=True,
                         error_stage=state.snapshot().get('stage'),
                         uncertain_starts='FULL_RESERVATION_RETAINED',
                         worker_completion_confirmed=False)
            _write_private_json(out/'coordinator-terminal.json', final)
            return final
        new_phase = 'bounded_cleanup_or_drain' if wall() >= window['execution_end_epoch'] else 'execution'
        if new_phase != phase:
            phase = new_phase
            snapshot({
                'phase':phase, 'worker_stage':state.snapshot(), 'recovery':recovery,
                'claim_reusable':False, 'uncertain_starts':'FULL_RESERVATION_RETAINED'})
        try:
            kind, payload = mailbox.get(timeout=min(.05, remaining))
        except queue.Empty:
            continue
        if kind == 'recovery':
            recovery.append(payload)
            snapshot({
                'phase':phase, 'worker_stage':state.snapshot(), 'recovery':recovery,
                'claim_reusable':False, 'uncertain_starts':'FULL_RESERVATION_RETAINED'})
        elif min(window['cleanup_end_epoch']-wall(), end-clock()) > 0:
            terminal.set()
            _write_private_json(out/'coordinator-terminal.json', payload)
            return payload
        # A completion at/after the boundary is not allowed to upgrade failure.
