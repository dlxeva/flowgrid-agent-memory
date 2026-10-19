"""Private, finite OpenAI cue budget. No credential discovery or network I/O.

Amounts are conservative uncached-price estimates in millionths of USD, not
provider invoices. Outstanding calls retain their entire hold after a crash.
An unknown outcome stops the scope; there is deliberately no reset API.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

MODEL = 'gpt-4o-mini-2024-07-18'
_SHA = re.compile(r'[0-9a-f]{64}\Z')


class BudgetStopped(ValueError):
    """Sanitized failure safe to handle without logging private inputs."""


@dataclass(frozen=True)
class BudgetPolicy:
    scope: str
    implementation_sha256: str
    profile_sha256: str
    budget_microusd: int
    max_attempts: int
    input_token_limit: int = 32768
    output_token_limit: int = 768
    input_bytes_limit: int = 16384
    model: str = MODEL

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,96}', self.scope):
            raise ValueError('budget_scope')
        if not all(isinstance(v, str) and _SHA.fullmatch(v) for v in
                   (self.implementation_sha256, self.profile_sha256)):
            raise ValueError('budget_identity')
        for field in ('budget_microusd', 'max_attempts', 'input_token_limit',
                      'output_token_limit', 'input_bytes_limit'):
            if type(getattr(self, field)) is not int or getattr(self, field) <= 0:
                raise ValueError('budget_positive_integer')
        if (self.model != MODEL or self.input_token_limit != 32768 or
                self.output_token_limit != 768 or self.input_bytes_limit != 16384):
            raise ValueError('budget_protocol_drift')

    @staticmethod
    def charge(prompt, completion):
        # Fixed standard price: $0.15/M input, $0.60/M output. No cache discount.
        return (prompt * 15 + completion * 60 + 99) // 100

    @property
    def hold(self):
        return self.charge(self.input_token_limit, self.output_token_limit)

    @property
    def binding(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


class ModelBudget:
    def __init__(self, path, policy, *, enabled=False):
        if enabled is not True:
            raise BudgetStopped('budget_disabled')
        if not isinstance(policy, BudgetPolicy):
            raise ValueError('budget_configuration')
        self.policy = policy
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError('budget_configuration')
        # A dedicated pre-existing private directory is required. Never chmod
        # an unrelated directory or follow a symlink to an existing ledger.
        for parent in (self.path.parent, *self.path.parent.parents):
            if parent.is_symlink():
                raise ValueError('budget_symlink')
        info = self.path.parent.stat()
        if os.name == 'posix' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('budget_directory_private')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    (os.name == 'posix' and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077))):
                raise ValueError('budget_file_private')
        finally:
            os.close(fd)
        with self._transaction() as con:
            con.execute('CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS calls (token TEXT PRIMARY KEY, request_sha TEXT NOT NULL, status TEXT NOT NULL, amount INTEGER NOT NULL, prompt INTEGER, completion INTEGER, response_sha TEXT)')
            con.execute('CREATE TABLE IF NOT EXISTS halt (id INTEGER PRIMARY KEY CHECK(id=1))')
            con.execute('INSERT OR IGNORE INTO policy VALUES (1, ?)', (policy.binding,))
            if con.execute('SELECT binding FROM policy WHERE id=1').fetchone()[0] != policy.binding:
                raise BudgetStopped('budget_binding_drift')

    @contextmanager
    def _transaction(self):
        con = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            con.execute('PRAGMA synchronous=FULL')
            con.execute('BEGIN IMMEDIATE')
            # Check on every callback, including after process restart.
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE name='policy'").fetchone()
            if exists:
                row = con.execute('SELECT binding FROM policy WHERE id=1').fetchone()
                if row and row[0] != self.policy.binding:
                    raise BudgetStopped('budget_binding_drift')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def reserve(self, request_sha, model, input_bytes, output_tokens):
        p = self.policy
        if (not isinstance(request_sha, str) or not _SHA.fullmatch(request_sha) or
                model != p.model or type(input_bytes) is not int or input_bytes != p.input_bytes_limit or
                type(output_tokens) is not int or output_tokens != p.output_token_limit):
            raise BudgetStopped('budget_request_drift')
        with self._transaction() as con:
            if con.execute('SELECT 1 FROM halt').fetchone():
                raise BudgetStopped('budget_halted')
            count, amount = con.execute('SELECT count(*), coalesce(sum(amount),0) FROM calls').fetchone()
            # Serial provider calls: a pending worker or unresolved crash blocks
            # reservations even in another process. No automatic retry/recovery.
            if con.execute("SELECT 1 FROM calls WHERE status!='settled' LIMIT 1").fetchone():
                raise BudgetStopped('budget_unresolved')
            if count >= p.max_attempts or amount + p.hold > p.budget_microusd:
                raise BudgetStopped('budget_limit')
            token = uuid.uuid4().hex
            con.execute('INSERT INTO calls(token,request_sha,status,amount) VALUES (?,?,?,?)',
                        (token, request_sha, 'reserved', p.hold))
            return token

    def settle(self, token, usage, response_sha):
        if (not isinstance(usage, dict) or not isinstance(response_sha, str) or
                not _SHA.fullmatch(response_sha)):
            raise BudgetStopped('budget_receipt')
        pt, ct, total = (usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
        if (any(type(v) is not int or v < 0 for v in (pt, ct, total)) or
                pt > self.policy.input_token_limit or ct > self.policy.output_token_limit or total != pt + ct):
            raise BudgetStopped('budget_receipt')
        amount = self.policy.charge(pt, ct)
        with self._transaction() as con:
            row = con.execute('SELECT status,amount,prompt,completion,response_sha FROM calls WHERE token=?', (token,)).fetchone()
            if row == ('settled', amount, pt, ct, response_sha):
                return  # Identical receipt replay is harmless and never spends.
            if row is None or row[0] != 'reserved':
                raise BudgetStopped('budget_settlement_state')
            con.execute("UPDATE calls SET status='settled',amount=?,prompt=?,completion=?,response_sha=? WHERE token=?",
                        (amount, pt, ct, response_sha, token))

    def unknown(self, token):
        with self._transaction() as con:
            row = con.execute('SELECT status FROM calls WHERE token=?', (token,)).fetchone()
            if row is None:
                raise BudgetStopped('budget_unknown_token')
            # A validated paid receipt remains settled if later semantic
            # validation fails. The service owns its quality failure state.
            if row[0] == 'reserved':
                con.execute("UPDATE calls SET status='unknown' WHERE token=?", (token,))

    def halt(self):
        """Persist a provider/semantic failure without erasing known receipts."""
        with self._transaction() as con:
            con.execute('INSERT OR IGNORE INTO halt VALUES (1)')

    def snapshot(self):
        with self._transaction() as con:
            count, amount = con.execute('SELECT count(*),coalesce(sum(amount),0) FROM calls').fetchone()
            pending = con.execute("SELECT count(*) FROM calls WHERE status!='settled'").fetchone()[0]
            return {'attempts': count, 'charged_or_held_microusd': amount,
                    'unresolved': pending, 'budget_microusd': self.policy.budget_microusd,
                    'halted': con.execute('SELECT 1 FROM halt').fetchone() is not None}
