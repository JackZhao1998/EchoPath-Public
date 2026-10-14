"""Cross-process run registry and cooperative controls for CLI, MCP and dashboard."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .run_steps import StepLog
from .platform_utils import process_alive

ACTIVE = ('queued', 'running', 'pausing', 'paused', 'stopping', 'resetting', 'awaiting_native')
current_control = contextvars.ContextVar('echopath_control', default=None)


class RunStopped(BaseException):
    """Bypass agent error recovery when the user stops a run."""


class DesktopResumed(BaseException):
    """Unwind stale GUI plans and replay coordinates after a pause."""


class RunRegistry:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        self.root = Path(config_path).resolve().parent / 'local_data'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'runs.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, pid INTEGER, target TEXT, task TEXT, status TEXT, desired TEXT, started REAL, updated REAL, detail TEXT, events TEXT)')
            # One readable line per desktop step, kept apart so the runs schema stays as it is.
            db.execute('CREATE TABLE IF NOT EXISTS run_steps (run_id TEXT PRIMARY KEY, steps TEXT)')
            # Every event of a run, for the full trace; runs.events keeps only the latest 40.
            db.execute('CREATE TABLE IF NOT EXISTS run_events (run_id TEXT, at TEXT, name TEXT, detail TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS run_events_run ON run_events (run_id)')
            # A worker's pending native segment and the host's answer; the worker keeps the token.
            db.execute('CREATE TABLE IF NOT EXISTS run_native (run_id TEXT PRIMARY KEY, handoff TEXT, answer TEXT)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM runs ORDER BY started DESC LIMIT 100').fetchall()
            result = []
            for row in rows:
                run = dict(row)
                if run['status'] in ACTIVE and int(run['pid']) > 0:
                    if not process_alive(run['pid']):
                        run.update(status='interrupted', detail='Worker exited. Start a new run to continue.')
                        db.execute('UPDATE runs SET status=?, detail=? WHERE id=?', (run['status'], run['detail'], run['id']))
                run['events'] = json.loads(run['events'])
                result.append(run)
            if result:
                marks = ','.join('?' * len(result))
                steps = dict(db.execute(f'SELECT run_id, steps FROM run_steps WHERE run_id IN ({marks})',
                                        [r['id'] for r in result]).fetchall())
                for run in result:
                    run['steps'] = json.loads(steps.get(run['id']) or '[]')
                    if run['status'] == 'awaiting_native':
                        row = db.execute('SELECT handoff FROM run_native WHERE run_id=? AND answer IS NULL', (run['id'],)).fetchone()
                        if row:
                            run['native_handoff'] = json.loads(row['handoff'])
            return result

    def trace(self, run_id):
        """Every recorded event of one run, oldest first (older runs fall back to their last 40)."""
        with self.connect() as db:
            rows = db.execute('SELECT at, name, detail FROM run_events WHERE run_id=? ORDER BY rowid', (run_id,)).fetchall()
        if rows:
            return [{'timestamp': r['at'], 'event': r['name'], 'detail': r['detail']} for r in rows]
        return [{'timestamp': e.get('timestamp'), 'event': '', 'detail': e.get('detail')} for e in self.get(run_id)['events']]

    def get(self, run_id):
        for run in self.list():
            if run['id'] == run_id:
                return run
        raise ValueError('Task was not found.')

    def create(self, task, target, run_id=None):
        run_id = run_id or uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute(
                'INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                (run_id, 0, target, task, 'queued', 'running', now, now, 'Queued', '[]'),
            )
        return run_id

    def set_error(self, run_id, detail):
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status='error', detail=?, updated=? WHERE id=?",
                (str(detail), time.time(), run_id),
            )

    def offer_native(self, run_id, handoff):
        """Publish a tracked worker's pending native segment for the host that started it."""
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO run_native VALUES (?, ?, NULL)', (run_id, json.dumps(handoff, default=str)))

    def answer_native(self, run_id, outputs=None, cancel=False):
        """Hand the worker the native segment's outputs, or cancel it; the worker verifies the live state."""
        if not cancel and (not isinstance(outputs or {}, dict) or any(not isinstance(v, str) for v in (outputs or {}).values())):
            raise ValueError('Outputs must be the declared string values.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
            row = db.execute('SELECT handoff FROM run_native WHERE run_id=? AND answer IS NULL', (run_id,)).fetchone()
            if run is None or run['status'] != 'awaiting_native' or row is None:
                raise ValueError('This run is not waiting for a native segment.')
            answer = {'segment_id': json.loads(row['handoff']).get('segment_id'), 'outputs': dict(outputs or {}), 'cancel': bool(cancel)}
            db.execute('UPDATE run_native SET answer=? WHERE run_id=?', (json.dumps(answer), run_id))

    def take_native_answer(self, run_id):
        """Consume the host's answer once; None while the worker should keep waiting."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT answer FROM run_native WHERE run_id=? AND answer IS NOT NULL', (run_id,)).fetchone()
            if row is None:
                return None
            db.execute('DELETE FROM run_native WHERE run_id=?', (run_id,))
            return json.loads(row['answer'])

    def request(self, run_id, action):
        target = self.get(run_id)['target']
        with input_lock(self.root, target):
            return self._request(run_id, action)

    def _request(self, run_id, action):
        desired = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped'}.get(action)
        if desired is None:
            raise ValueError('Unknown control action.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None or row['status'] not in ACTIVE:
                raise ValueError('This run is no longer active.')
            if row['status'] == 'awaiting_native' and action == 'stop':
                # A tracked worker is waiting on its host; stopping cancels that segment.
                pending = db.execute('SELECT handoff FROM run_native WHERE run_id=? AND answer IS NULL', (run_id,)).fetchone()
                if pending:
                    answer = {'segment_id': json.loads(pending['handoff']).get('segment_id'), 'outputs': {}, 'cancel': True}
                    db.execute('UPDATE run_native SET answer=? WHERE run_id=?', (json.dumps(answer), run_id))
                    return
            if row['status'] in ('resetting', 'awaiting_native'):
                raise ValueError('Wait for VM reset, or finish/cancel the native continuation in its session.')
            if action == 'resume' and row['status'] != 'paused':
                raise ValueError('Wait until the run is paused before resuming.')
            if row['status'] == 'stopping':
                raise ValueError('This run is already stopping.')
            if action == 'pause' and row['status'] == 'paused':
                return
            status = 'pausing' if action == 'pause' else 'stopping' if action == 'stop' else 'paused'
            db.execute('UPDATE runs SET desired=?, status=?, updated=? WHERE id=?', (desired, status, time.time(), run_id))

    @contextmanager
    def manual_input(self, target):
        """Serialize input against resume/reset; an idle VM also needs its desktop lease."""
        with input_lock(self.root, target):
            runs = [r for r in self.list() if r['target'].rstrip('/') == target.rstrip('/') and r['status'] in ACTIVE]
            if runs:
                if any(r['status'] != 'paused' or r['desired'] != 'paused' for r in runs):
                    raise ValueError('Pause the VM task and wait until it is paused before taking control.')
                yield
            else:
                with desktop_lock(self.root, target):
                    yield


@contextmanager
def desktop_lock(root, target, *, blocking=False):
    """Shared by task execution and snapshot maintenance, across processes."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(target.rstrip('/').encode()).hexdigest()[:20]
    lock = (root / (lock_name + '.lock')).open('a')
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                lock.write('0')
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            raise RuntimeError('A run already owns this desktop, including paused runs or VM reset. Stop it or resume it first.') from None
        yield lock
    finally:
        lock.close()


def input_lock(root, target):
    return desktop_lock(root, 'manual-input:' + target.rstrip('/'), blocking=True)


class RunControl:
    def __init__(self, registry, task, target, run_id=None):
        self.registry, self.task, self.target = registry, task, target
        self.id = run_id or uuid.uuid4().hex
        self.desktop_scope = False
        self.steps = StepLog()
        self.vm_reset = None

    def __enter__(self):
        self._lease = desktop_lock(self.registry.root, self.target)
        self.lock = self._lease.__enter__()
        now = time.time()
        with self.registry.connect() as db:
            existing = db.execute('SELECT desired FROM runs WHERE id=?', (self.id,)).fetchone()
            if existing:
                status = {'stopped': 'stopping', 'paused': 'pausing'}.get(existing['desired'], 'running')
                db.execute("UPDATE runs SET pid=?, status=?, updated=?, detail='Starting task' WHERE id=?", (os.getpid(), status, now, self.id))
            else:
                db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)', (self.id, os.getpid(), self.target, self.task, 'running', 'running', now, now, 'Starting task', '[]'))
        self.token = current_control.set(self)
        try:
            if self.target != 'local':
                from .vm_setup import VMSetup
                VMSetup(self.registry.config_path).guard(self.target)
            from .vm_reset import VMReset
            self.vm_reset = VMReset.configured(self.registry.config_path, self.target)
            if self.vm_reset:
                self.vm_reset.before_run(self)
        except BaseException as error:
            self.registry.set_error(self.id, str(error))
            current_control.reset(self.token)
            self._lease.__exit__(None, None, None)
            raise
        return self

    def update(self, detail, event=None, steps=None):
        changed = self.steps.merge(steps, event)
        with self.registry.connect() as db:
            row = db.execute('SELECT events FROM runs WHERE id=?', (self.id,)).fetchone()
            events = json.loads(row['events'])
            if event:
                events.append({'timestamp': event.timestamp.isoformat(), 'detail': detail})
                db.execute('INSERT INTO run_events VALUES (?, ?, ?, ?)',
                           (self.id, event.timestamp.isoformat(), str(getattr(event, 'name', '') or ''), str(detail or '')))
            db.execute('UPDATE runs SET detail=?, updated=?, events=? WHERE id=?', (detail, time.time(), json.dumps(events[-40:]), self.id))
            if changed:
                db.execute('INSERT OR REPLACE INTO run_steps VALUES (?, ?)', (self.id, json.dumps(self.steps.as_list())))

    async def checkpoint(self):
        paused = False
        while True:
            with self.registry.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT desired FROM runs WHERE id=?', (self.id,)).fetchone()
                if row['desired'] == 'stopped':
                    raise RunStopped('Stopped by you.')
                if row['desired'] == 'running':
                    db.execute("UPDATE runs SET status='running' WHERE id=?", (self.id,))
                    break
                paused = True
                db.execute("UPDATE runs SET status='paused', detail='Paused — you can use this desktop now.' WHERE id=?", (self.id,))
            await asyncio.sleep(.15)
        if paused:
            self.update('Resuming — observing the current screen again.')
            if self.desktop_scope:
                raise DesktopResumed()

    def finish(self, status, detail):
        with self.registry.connect() as db:
            db.execute('UPDATE runs SET status=?, detail=?, updated=? WHERE id=?', (status, detail, time.time(), self.id))
            if self.steps.finish(status):
                db.execute('INSERT OR REPLACE INTO run_steps VALUES (?, ?)', (self.id, json.dumps(self.steps.as_list())))

    def __exit__(self, kind, error, tb):
        try:
            if kind:
                self.finish('stopped' if isinstance(error, (RunStopped, KeyboardInterrupt)) else 'error', str(error) or 'Stopped by you.')
            if self.vm_reset:
                self.vm_reset.after_run(self)
        finally:
            current_control.reset(self.token)
            self._lease.__exit__(None, None, None)


async def checkpoint():
    control = current_control.get()
    if control:
        await control.checkpoint()
