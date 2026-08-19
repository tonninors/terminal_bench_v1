"""Deterministic synthetic ledger schema and data.

Everything here is invented for this benchmark: identifiers, names, currencies,
free text and blob payloads are produced from a fixed-seed PRNG over
self-defined syllable/word tables.  No external dataset is read or embedded.
"""
from __future__ import annotations

import hashlib
import random

PAGE_SIZE = 4096

SCHEMA_SQL = """
CREATE TABLE accounts (
    account_id    INTEGER PRIMARY KEY,
    account_no    TEXT    NOT NULL UNIQUE,
    owner_name    TEXT    NOT NULL,
    currency      TEXT    NOT NULL CHECK (length(currency) = 3),
    status        TEXT    NOT NULL CHECK (status IN ('open','frozen','closed')),
    opened_on     TEXT    NOT NULL,
    balance_minor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE journal_entries (
    entry_id   INTEGER PRIMARY KEY,
    entry_uuid TEXT    NOT NULL UNIQUE,
    posted_on  TEXT    NOT NULL,
    state      TEXT    NOT NULL CHECK (state IN ('pending','posted','void')),
    fx_rate    REAL,
    narrative  TEXT    NOT NULL,
    memo       TEXT,
    payload    BLOB
);

CREATE TABLE postings (
    entry_id     INTEGER NOT NULL REFERENCES journal_entries(entry_id) ON DELETE CASCADE,
    leg_no       INTEGER NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
    amount_minor INTEGER NOT NULL,
    leg_note     TEXT,
    PRIMARY KEY (entry_id, leg_no)
) WITHOUT ROWID;

CREATE TABLE audit_log (
    audit_id    INTEGER PRIMARY KEY,
    table_name  TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    action      TEXT NOT NULL,
    delta_minor INTEGER,
    note        TEXT
);

CREATE UNIQUE INDEX ux_accounts_owner_ccy ON accounts(owner_name, currency);
CREATE INDEX ix_postings_account ON postings(account_id, entry_id);
CREATE INDEX ix_entries_posted_on ON journal_entries(posted_on);
CREATE INDEX ix_entries_open_work ON journal_entries(posted_on, entry_id)
    WHERE state = 'pending';
CREATE INDEX ix_audit_target ON audit_log(table_name, row_key);

CREATE VIEW account_balances AS
    SELECT a.account_id            AS account_id,
           a.account_no            AS account_no,
           a.currency              AS currency,
           a.balance_minor         AS stored_minor,
           COALESCE(SUM(p.amount_minor), 0) AS derived_minor
      FROM accounts a
      LEFT JOIN postings p ON p.account_id = a.account_id
     GROUP BY a.account_id, a.account_no, a.currency, a.balance_minor;

CREATE VIEW pending_workload AS
    SELECT e.entry_id, e.entry_uuid, e.posted_on, COUNT(p.leg_no) AS legs
      FROM journal_entries e
      LEFT JOIN postings p ON p.entry_id = e.entry_id
     WHERE e.state = 'pending'
     GROUP BY e.entry_id, e.entry_uuid, e.posted_on;

CREATE TRIGGER trg_postings_ai AFTER INSERT ON postings
BEGIN
    UPDATE accounts
       SET balance_minor = balance_minor + NEW.amount_minor
     WHERE account_id = NEW.account_id;
    INSERT INTO audit_log(table_name, row_key, action, delta_minor, note)
    VALUES ('postings', NEW.entry_id || ':' || NEW.leg_no, 'insert',
            NEW.amount_minor, NEW.leg_note);
END;

CREATE TRIGGER trg_postings_ad AFTER DELETE ON postings
BEGIN
    UPDATE accounts
       SET balance_minor = balance_minor - OLD.amount_minor
     WHERE account_id = OLD.account_id;
    INSERT INTO audit_log(table_name, row_key, action, delta_minor, note)
    VALUES ('postings', OLD.entry_id || ':' || OLD.leg_no, 'delete',
            -OLD.amount_minor, OLD.leg_note);
END;

CREATE TRIGGER trg_entries_state_au AFTER UPDATE OF state ON journal_entries
WHEN OLD.state <> NEW.state
BEGIN
    INSERT INTO audit_log(table_name, row_key, action, delta_minor, note)
    VALUES ('journal_entries', CAST(NEW.entry_id AS TEXT), 'state',
            NULL, OLD.state || '->' || NEW.state);
END;

CREATE TRIGGER trg_accounts_status_au AFTER UPDATE OF status ON accounts
WHEN OLD.status <> NEW.status
BEGIN
    INSERT INTO audit_log(table_name, row_key, action, delta_minor, note)
    VALUES ('accounts', CAST(NEW.account_id AS TEXT), 'status',
            NULL, OLD.status || '->' || NEW.status);
END;
"""

_SYL_A = ["kar", "vel", "nor", "tis", "mal", "quor", "sen", "dra", "lun", "beth",
          "fyn", "orv", "sil", "tam", "rhe", "gol", "esk", "una", "prim", "zad"]
_SYL_B = ["ic", "an", "os", "ell", "ur", "ith", "ay", "on", "ex", "ara",
          "ov", "een", "ual", "ist", "orn", "ade", "yne", "ilo", "ent", "uxa"]
_WORDS = ["reconcile", "settle", "clearing", "batch", "residual", "accrual",
          "fx", "spread", "netting", "cutover", "haircut", "sweep", "custody",
          "nostro", "vostro", "tranche", "coupon", "rebate", "escrow", "margin",
          "carry", "rollup", "posting", "reversal", "provision", "float"]
_CCY = ["EUR", "USD", "GBP", "CHF", "SEK", "NOK", "JPY", "CAD"]


def _name(rng: random.Random, idx: int) -> str:
    a = _SYL_A[(idx * 7 + rng.randrange(len(_SYL_A))) % len(_SYL_A)]
    b = _SYL_B[(idx * 11 + rng.randrange(len(_SYL_B))) % len(_SYL_B)]
    c = _SYL_A[(idx * 13 + rng.randrange(len(_SYL_A))) % len(_SYL_A)]
    return f"{a.capitalize()}{b} {c.capitalize()}{idx:03d}"


def _sentence(rng: random.Random, nwords: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(nwords))


def _date(rng: random.Random, base_day: int) -> str:
    day = base_day + rng.randrange(0, 40)
    y = 2031 + day // 360
    m = (day // 30) % 12 + 1
    d = day % 28 + 1
    return f"{y:04d}-{m:02d}-{d:02d}"


def entry_uuid(n: int) -> str:
    """Deterministic, content-free identifier.  Deliberately carries no marker
    telling which transaction created the row."""
    h = hashlib.sha256(f"ledger-entry-{n}".encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def make_accounts(rng: random.Random, n: int) -> list[tuple]:
    rows = []
    used = set()
    for i in range(1, n + 1):
        while True:
            owner = _name(rng, i)
            ccy = _CCY[rng.randrange(len(_CCY))]
            if (owner, ccy) not in used:
                used.add((owner, ccy))
                break
        rows.append((
            i,
            f"AC-{2031000 + i * 37:07d}",
            owner,
            ccy,
            ("open", "open", "open", "frozen", "closed")[rng.randrange(5)],
            _date(rng, 0),
            0,
        ))
    return rows


def make_entry(rng: random.Random, entry_id: int, base_day: int) -> tuple:
    memo_len = rng.choice([180, 640, 1500, 2600, 4200, 5600])
    memo = _sentence(rng, memo_len // 8)[:memo_len]
    if rng.random() < 0.30:
        payload = None
    else:
        plen = rng.choice([48, 220, 900, 2400])
        payload = bytes(rng.randrange(256) for _ in range(plen))
    fx = None if rng.random() < 0.25 else round(0.5 + rng.random() * 2.5, 6)
    state = ("pending", "posted", "posted", "posted")[rng.randrange(4)]
    return (
        entry_id,
        entry_uuid(entry_id),
        _date(rng, base_day),
        state,
        fx,
        _sentence(rng, 6),
        memo,
        payload,
    )


def make_legs(rng: random.Random, entry_id: int, n_accounts: int) -> list[tuple]:
    a1 = rng.randrange(1, n_accounts + 1)
    a2 = rng.randrange(1, n_accounts + 1)
    while a2 == a1:
        a2 = rng.randrange(1, n_accounts + 1)
    amt = rng.randrange(100, 5_000_00)
    return [
        (entry_id, 1, a1, amt, _sentence(rng, 3)),
        (entry_id, 2, a2, -amt, _sentence(rng, 3)),
    ]
