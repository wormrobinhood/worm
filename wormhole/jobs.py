"""Durable scan jobs for the single scoring worker. No signing or external calls."""
import time

MAX_ATTEMPTS = 3
LEASE_S = 1800


def enqueue(db, token, now=None):
    now = int(time.time()) if now is None else now
    with db.transaction():
        db.x("INSERT OR IGNORE INTO scan_jobs(token,available_at) VALUES(?,?)", (token, now))
        # Explicit requests may rescan completed work; pending/leased jobs are deduplicated.
        db.x("UPDATE scan_jobs SET state='pending', attempts=0, available_at=?, lease_until=NULL,"
             " generation=generation+1 WHERE token=? AND state IN ('done','failed')", (now, token))


class ScanQueue:
    def __init__(self, db):
        self.db = db

    def put(self, token):
        enqueue(self.db, token)

    def claim(self, now=None):
        now = int(time.time()) if now is None else now
        with self.db.transaction():
            # A crashed final attempt is visible as failed, not silently forgotten.
            self.db.x("UPDATE scan_jobs SET state='failed', lease_until=NULL WHERE state='leased'"
                      " AND lease_until<=? AND attempts>=?", (now, MAX_ATTEMPTS))
            row = self.db.one("SELECT * FROM scan_jobs WHERE (state='pending' AND available_at<=?)"
                              " OR (state='leased' AND lease_until<=? AND attempts<?) ORDER BY available_at, token LIMIT 1",
                              (now, now, MAX_ATTEMPTS))
            if not row:
                return None
            self.db.x("UPDATE scan_jobs SET state='leased', attempts=attempts+1, lease_until=? WHERE token=?",
                      (now + LEASE_S, row['token']))
            row['attempts'] += 1
            return row

    def finish(self, job, success, retry=False, now=None):
        now = int(time.time()) if now is None else now
        state = 'pending' if (retry and job['attempts'] < MAX_ATTEMPTS) else ('done' if success and not retry else 'failed')
        self.db.x("UPDATE scan_jobs SET state=?, available_at=?, lease_until=NULL WHERE token=?"
                  " AND state='leased' AND generation=? AND attempts=?",
                  (state, now + 600, job['token'], job['generation'], job['attempts']))

    def renew(self, job):
        self.db.x("UPDATE scan_jobs SET lease_until=? WHERE token=? AND state='leased' AND generation=? AND attempts=?",
                  (int(time.time()) + LEASE_S, job['token'], job['generation'], job['attempts']))
