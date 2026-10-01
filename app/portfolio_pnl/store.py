"""SQLite storage for GMGN Recent PnL and holdings snapshots."""
import hashlib
import json
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Dict, List, Optional

VERSION = '1.2.0'
BASE = Path(__file__).resolve().parents[2]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


_COMPRESSED_JSON_MAGIC = b'CTZ1'


def encode_payload(value):
    """Store large JSON payloads compressed while keeping small rows readable."""
    text = encode(value)
    raw = text.encode('utf-8')
    if len(raw) < 512:
        return text
    compressed = _COMPRESSED_JSON_MAGIC + zlib.compress(raw, level=6)
    return sqlite3.Binary(compressed) if len(compressed) < len(raw) * 0.9 else text


def decode_payload(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if raw.startswith(_COMPRESSED_JSON_MAGIC):
            raw = zlib.decompress(raw[len(_COMPRESSED_JSON_MAGIC):])
        return json.loads(raw.decode('utf-8'))
    return json.loads(value)


class PnlStore:
    def __init__(self, path=None):
        self.path = Path(path or BASE / 'data' / 'wallet_portfolio_pnl.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS wallets(
              address TEXT PRIMARY KEY,
              name TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crawl_runs(
              id INTEGER PRIMARY KEY,
              wallet TEXT NOT NULL,
              source TEXT NOT NULL,
              started_at INTEGER NOT NULL,
              ended_at INTEGER,
              status TEXT NOT NULL,
              params TEXT NOT NULL,
              error TEXT,
              pages INTEGER DEFAULT 0,
              rows INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS raw_pages(
              id INTEGER PRIMARY KEY,
              run_id INTEGER NOT NULL,
              wallet TEXT NOT NULL,
              source TEXT NOT NULL,
              endpoint TEXT NOT NULL,
              params TEXT NOT NULL,
              fetched_at INTEGER NOT NULL,
              sha256 TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pnl_raw_wallet ON raw_pages(wallet, source);
            CREATE INDEX IF NOT EXISTS pnl_raw_endpoint_time
              ON raw_pages(wallet, endpoint, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS pnl_raw_source_endpoint_time
              ON raw_pages(wallet, source, endpoint, fetched_at DESC);
            CREATE INDEX IF NOT EXISTS pnl_raw_run
              ON raw_pages(run_id);
            CREATE TABLE IF NOT EXISTS token_rows(
              id INTEGER PRIMARY KEY,
              wallet TEXT NOT NULL,
              crawl_run_id INTEGER NOT NULL,
              token_address TEXT NOT NULL,
              fetched_at INTEGER NOT NULL,
              row_hash TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pnl_token_wallet ON token_rows(wallet, fetched_at DESC);
            CREATE TABLE IF NOT EXISTS wallet_summaries(
              wallet TEXT PRIMARY KEY,
              crawl_run_id INTEGER NOT NULL,
              fetched_at INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dashboard_snapshots(
              wallet TEXT PRIMARY KEY,
              fetched_at INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intraday_snapshots(
              wallet TEXT PRIMARY KEY,
              fetched_at INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_activity_snapshots(
              wallet TEXT PRIMARY KEY,
              fetched_at INTEGER NOT NULL,
              start_at INTEGER NOT NULL,
              end_at INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intraday_backfill_jobs(
              wallet TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              days INTEGER NOT NULL,
              pages INTEGER NOT NULL DEFAULT 0,
              events INTEGER NOT NULL DEFAULT 0,
              started_at INTEGER,
              finished_at INTEGER,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS wallet_feature_runs(
              id INTEGER PRIMARY KEY,
              wallet TEXT NOT NULL,
              feature_version TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              computed_at INTEGER NOT NULL,
              payload TEXT NOT NULL,
              UNIQUE(wallet,feature_version,input_hash)
            );
            CREATE INDEX IF NOT EXISTS feature_runs_wallet
              ON wallet_feature_runs(wallet,computed_at DESC);
            CREATE TABLE IF NOT EXISTS wallet_label_runs(
              id INTEGER PRIMARY KEY,
              wallet TEXT NOT NULL,
              formula_version TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              feature_run_id INTEGER,
              computed_at INTEGER NOT NULL,
              payload TEXT NOT NULL,
              UNIQUE(wallet,formula_version,input_hash)
            );
            CREATE INDEX IF NOT EXISTS label_runs_wallet
              ON wallet_label_runs(wallet,computed_at DESC);
            CREATE TABLE IF NOT EXISTS external_data_snapshots(
              provider TEXT NOT NULL,
              kind TEXT NOT NULL,
              wallet TEXT NOT NULL,
              fetched_at INTEGER NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL,
              PRIMARY KEY(provider,kind,wallet)
            );
            CREATE INDEX IF NOT EXISTS external_snapshots_wallet
              ON external_data_snapshots(wallet,provider);
            CREATE TABLE IF NOT EXISTS onchain_tx_cache(
              signature TEXT PRIMARY KEY,
              fetched_at INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wallet_refresh_state(
              wallet TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              last_attempt_at INTEGER,
              last_success_at INTEGER,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              next_retry_at INTEGER NOT NULL DEFAULT 0,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS wallet_refresh_components(
              wallet TEXT NOT NULL,
              component TEXT NOT NULL,
              status TEXT NOT NULL,
              last_attempt_at INTEGER,
              last_success_at INTEGER,
              error TEXT,
              PRIMARY KEY(wallet,component)
            );
            CREATE TABLE IF NOT EXISTS wallet_refresh_queue(
              wallet TEXT PRIMARY KEY,
              priority INTEGER NOT NULL DEFAULT 0,
              requested_at INTEGER NOT NULL,
              reason TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS refresh_queue_priority
              ON wallet_refresh_queue(priority DESC, requested_at ASC);
            CREATE TABLE IF NOT EXISTS wallet_component_watermarks(
              wallet TEXT NOT NULL,
              component TEXT NOT NULL,
              provider_cursor TEXT,
              oldest_timestamp INTEGER,
              newest_timestamp INTEGER,
              coverage_days INTEGER,
              history_exhausted INTEGER NOT NULL DEFAULT 0,
              payload_hash TEXT,
              status TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(wallet,component)
            );
            CREATE TABLE IF NOT EXISTS wallet_events(
              wallet TEXT NOT NULL,
              event_id TEXT NOT NULL,
              timestamp INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              tx_hash TEXT NOT NULL DEFAULT '',
              token_address TEXT NOT NULL DEFAULT '',
              payload_hash TEXT NOT NULL,
              first_seen_at INTEGER NOT NULL,
              last_seen_at INTEGER NOT NULL,
              payload TEXT NOT NULL,
              PRIMARY KEY(wallet,event_id)
            );
            CREATE INDEX IF NOT EXISTS wallet_events_time
              ON wallet_events(wallet,timestamp DESC);
            -- Canonical serving layer. Raw snapshots/pages remain immutable
            -- audit data; these tables contain the latest typed facts and the
            -- exact feature/label input used by the current classifier.
            CREATE TABLE IF NOT EXISTS canonical_wallet_state(
              wallet TEXT PRIMARY KEY,
              wallet_name TEXT NOT NULL DEFAULT '',
              wallet_emoji TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL,
              dashboard_fetched_at INTEGER,
              activity_fetched_at INTEGER,
              intraday_fetched_at INTEGER,
              holdings_complete INTEGER NOT NULL DEFAULT 0,
              activity_complete INTEGER NOT NULL DEFAULT 0,
              token_count INTEGER NOT NULL DEFAULT 0,
              event_count INTEGER NOT NULL DEFAULT 0,
              feature_version TEXT,
              formula_version TEXT,
              input_hash TEXT,
              data_status TEXT NOT NULL DEFAULT 'MISSING',
              last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS canonical_wallet_status
              ON canonical_wallet_state(data_status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS canonical_token_facts(
              wallet TEXT NOT NULL,
              token_address TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              token_symbol TEXT,
              token_name TEXT,
              token_launch_timestamp INTEGER,
              first_buy_timestamp INTEGER,
              first_buy_price REAL,
              market_cap_at_first_buy_usd REAL,
              first_buy_amount_usd REAL,
              buy_usd REAL,
              total_position_usd REAL,
              realized_pnl_usd REAL,
              entry_age_minutes REAL,
              position_action_span_hours REAL,
              first_sell_pct REAL,
              sell_100pct INTEGER,
              averaging_down INTEGER,
              volume_usd REAL,
              buy_order_usd_json TEXT,
              source_snapshot_at INTEGER,
              raw_fields_json TEXT,
              -- Hash of the typed token facts.  It lets realtime refreshes
              -- skip UPDATEs for observations that did not change.
              fact_hash TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(wallet,token_address)
            );
            CREATE INDEX IF NOT EXISTS canonical_token_wallet
              ON canonical_token_facts(wallet, updated_at DESC);
            CREATE INDEX IF NOT EXISTS canonical_token_profit
              ON canonical_token_facts(wallet, realized_pnl_usd DESC);
            CREATE TABLE IF NOT EXISTS canonical_features_current(
              wallet TEXT PRIMARY KEY,
              feature_version TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              computed_at INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canonical_labels_current(
              wallet TEXT PRIMARY KEY,
              formula_version TEXT NOT NULL,
              input_hash TEXT NOT NULL,
              computed_at INTEGER NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canonical_label_inputs_current(
              wallet TEXT PRIMARY KEY,
              wallet_name TEXT NOT NULL DEFAULT '',
              wallet_emoji TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL,
              data_status TEXT NOT NULL DEFAULT 'MISSING',
              feature_version TEXT,
              formula_version TEXT,
              input_hash TEXT,
              source TEXT NOT NULL DEFAULT 'derived',
              money_earned_primary_usd REAL,
              capital_base_usd REAL,
              current_portfolio_usd REAL,
              roi_on_trading_cost_pct REAL,
              pnl_over_deposits_pct REAL,
              jackpot_dependency_pct REAL,
              portfolio_max_drawdown_pct REAL,
              max_realized_pnl_drawdown_usd REAL,
              median_volume_last_30_tokens_usd REAL,
              median_buy_order_last_30_tokens_usd REAL,
              longest_loss_streak_tokens REAL,
              current_loss_streak_tokens REAL,
              withdrawal_count REAL,
              total_withdrawals_usd REAL,
              median_outflow_holding_pct REAL,
              median_transfer_holding_pct REAL,
              sell_100pct_rate REAL,
              transfer_100pct_rate REAL,
              multi_buy_token_rate_pct REAL,
              median_position_action_span_hours REAL,
              median_first_sell_pct REAL,
              averaging_down_token_rate_pct REAL,
              max_realized_pnl_single_token_usd REAL,
              lowcap_entry_count REAL,
              lowcap_entry_rate_pct REAL,
              early_entry_count REAL,
              early_entry_rate_pct REAL,
              early_lowcap_count REAL,
              early_lowcap_rate_pct REAL,
              median_entry_market_cap_usd REAL,
              median_entry_age_minutes REAL,
              token_sample REAL,
              median_volume REAL,
              median_buy_order REAL,
              whale INTEGER,
              multi_buy REAL,
              holding REAL,
              first_sell REAL,
              sell_100pct_conviction REAL,
              averaging_down REAL,
              conviction INTEGER,
              conviction_pass_count REAL,
              same_theme_ca_count REAL,
              same_theme_ca_share_pct REAL,
              largest_ca_share_in_theme REAL,
              theme_buy_window_hours REAL,
              is_basket_narrative INTEGER,
              holder_score REAL,
              group_labels_json TEXT NOT NULL DEFAULT '{}',
              legacy_labels_json TEXT NOT NULL DEFAULT '{}',
              value_sources_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS canonical_label_inputs_status
              ON canonical_label_inputs_current(data_status, updated_at DESC);
            ''')
            # Older databases created wallet_events with only a compressed
            # payload. Add typed event facts in place; raw provider pages remain
            # the audit copy, so the event serving table does not need to repeat
            # every JSON blob.
            existing_event_columns = {
                row[1] for row in db.execute('PRAGMA table_info(wallet_events)')
            }
            event_columns = {
                'token_symbol': 'TEXT',
                'token_amount': 'REAL',
                'amount_usd': 'REAL',
                'buy_cost_usd': 'REAL',
                'price_usd': 'REAL',
                'quote_amount': 'REAL',
                'quote_address': 'TEXT',
                'gas_usd': 'REAL',
            }
            for column, ddl in event_columns.items():
                if column not in existing_event_columns:
                    try:
                        db.execute(f'ALTER TABLE wallet_events ADD COLUMN {column} {ddl}')
                    except sqlite3.OperationalError as exc:
                        # Another process may have initialized this additive
                        # migration between PRAGMA and ALTER. Re-read the
                        # schema before surfacing a real migration failure.
                        if 'duplicate column name' not in str(exc).lower():
                            raise
            watermark_columns = {
                row[1] for row in db.execute(
                    'PRAGMA table_info(wallet_component_watermarks)'
                )
            }
            if 'history_exhausted' not in watermark_columns:
                try:
                    db.execute(
                        'ALTER TABLE wallet_component_watermarks '
                        'ADD COLUMN history_exhausted INTEGER NOT NULL DEFAULT 0'
                    )
                except sqlite3.OperationalError as exc:
                    if 'duplicate column name' not in str(exc).lower():
                        raise
            token_columns = {
                'buy_usd': 'REAL',
                'fact_hash': "TEXT NOT NULL DEFAULT ''",
            }
            existing_token_columns = {
                row[1] for row in db.execute('PRAGMA table_info(canonical_token_facts)')
            }
            for column, ddl in token_columns.items():
                if column not in existing_token_columns:
                    try:
                        db.execute(f'ALTER TABLE canonical_token_facts ADD COLUMN {column} {ddl}')
                    except sqlite3.OperationalError as exc:
                        if 'duplicate column name' not in str(exc).lower():
                            raise
            # Additive migration for databases created before identity fields
            # and the complete label-input serving table were introduced.
            for column, ddl in {'wallet_emoji': "TEXT NOT NULL DEFAULT ''"}.items():
                if column not in {
                    row[1] for row in db.execute('PRAGMA table_info(canonical_wallet_state)')
                }:
                    try:
                        db.execute(f'ALTER TABLE canonical_wallet_state ADD COLUMN {column} {ddl}')
                    except sqlite3.OperationalError as exc:
                        if 'duplicate column name' not in str(exc).lower():
                            raise

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        # WAL allows dashboard reads while the refresh worker commits a page or
        # component.  NORMAL is durable with WAL and avoids an fsync for every
        # small component write; busy_timeout prevents transient lock errors
        # when export and refresh overlap.
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA synchronous=NORMAL')
        db.execute('PRAGMA wal_autocheckpoint=1000')
        return db

    def add_wallet(self, address: str, name: str = ''):
        with self.connect() as db:
            db.execute(
                'INSERT INTO wallets VALUES(?,?,?) ON CONFLICT(address) DO UPDATE SET '
                "name=CASE WHEN excluded.name != '' THEN excluded.name ELSE wallets.name END",
                (address, name, int(time.time())),
            )

    def start_run(self, wallet: str, source: str, params: dict) -> int:
        with self.connect() as db:
            return db.execute(
                'INSERT INTO crawl_runs(wallet,source,started_at,status,params) VALUES(?,?,?,?,?)',
                (wallet, source, int(time.time()), 'running', encode(params)),
            ).lastrowid

    def finish_run(self, run_id: int, status: str, error: str = None, rows: int = 0):
        with self.connect() as db:
            db.execute(
                'UPDATE crawl_runs SET ended_at=?,status=?,error=?,rows=? WHERE id=?',
                (int(time.time()), status, error, rows, run_id),
            )

    def append_page(self, run_id: int, wallet: str, source: str, endpoint: str, params: dict, payload: dict, count: int = 0) -> int:
        body = encode(payload)
        with self.connect() as db:
            page_id = db.execute(
                'INSERT INTO raw_pages(run_id,wallet,source,endpoint,params,fetched_at,sha256,payload) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (run_id, wallet, source, endpoint, encode(params), int(time.time()),
                 hashlib.sha256(body.encode()).hexdigest(), encode_payload(payload)),
            ).lastrowid
            db.execute('UPDATE crawl_runs SET pages=pages+1,rows=rows+? WHERE id=?', (count, run_id))
            return page_id

    def append_pages(
        self, run_id: int, wallet: str, source: str, endpoint: str,
        pages: list[dict],
    ) -> int:
        """Append a page batch in one SQLite transaction.

        Crawls can contain hundreds of pages.  Opening and committing a new
        connection for every page makes WAL files grow and turns one refresh
        into hundreds of fsync/lock cycles.  The input shape matches the page
        dictionaries returned by the GMGN clients.
        """
        if not pages:
            return 0
        inserted = 0
        total_rows = 0
        with self.connect() as db:
            for page in pages:
                payload = page.get('payload') or {}
                params = page.get('params') or {}
                body = encode(payload)
                db.execute(
                    'INSERT INTO raw_pages(run_id,wallet,source,endpoint,params,fetched_at,sha256,payload) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (
                        run_id, wallet, source, endpoint,
                        encode(params), int(page.get('fetched_at') or time.time()),
                        hashlib.sha256(body.encode()).hexdigest(),
                        encode_payload(payload),
                    ),
                )
                total_rows += int(page.get('count') or 0)
                inserted += 1
            db.execute(
                'UPDATE crawl_runs SET pages=pages+?,rows=rows+? WHERE id=?',
                (inserted, total_rows, run_id),
            )
        return inserted

    def save_token_rows(self, wallet: str, run_id: int, rows: List[dict]):
        now = int(time.time())
        with self.connect() as db:
            values = []
            for row in rows:
                body = encode(row)
                values.append(
                    (wallet, run_id, row.get('token_address', ''), now,
                     hashlib.sha256(body.encode()).hexdigest(), encode_payload(row))
                )
            if values:
                db.executemany(
                    'INSERT INTO token_rows(wallet,crawl_run_id,token_address,fetched_at,row_hash,payload) '
                    'VALUES(?,?,?,?,?,?)',
                    values,
                )

    def save_wallet_summary(self, wallet: str, run_id: int, summary: dict):
        with self.connect() as db:
            db.execute(
                'INSERT INTO wallet_summaries(wallet,crawl_run_id,fetched_at,payload) VALUES(?,?,?,?) '
                'ON CONFLICT(wallet) DO UPDATE SET crawl_run_id=excluded.crawl_run_id,'
                'fetched_at=excluded.fetched_at,payload=excluded.payload',
                (wallet, run_id, int(time.time()), encode_payload(summary)),
            )

    def latest_token_rows(self, wallet: str) -> List[dict]:
        with self.connect() as db:
            run = db.execute(
                'SELECT crawl_run_id FROM token_rows WHERE wallet=? ORDER BY fetched_at DESC LIMIT 1',
                (wallet,),
            ).fetchone()
            if not run:
                return []
            rows = db.execute(
                'SELECT payload FROM token_rows WHERE wallet=? AND crawl_run_id=? ORDER BY id',
                (wallet, run['crawl_run_id']),
            ).fetchall()
        return [decode_payload(r['payload']) for r in rows]

    def latest_summary(self, wallet: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute('SELECT payload FROM wallet_summaries WHERE wallet=?', (wallet,)).fetchone()
        return decode_payload(row['payload']) if row else None

    def pages(self, wallet: str, source: str = 'gmgn'):
        with self.connect() as db:
            rows = db.execute(
                'SELECT * FROM raw_pages WHERE wallet=? AND source=? ORDER BY id',
                (wallet, source),
            ).fetchall()
        return [{**dict(r), 'payload': decode_payload(r['payload']), 'params': json.loads(r['params'])} for r in rows]

    def latest_complete_pages(self, wallet: str, source: str = 'gmgn', endpoint: str = None):
        with self.connect() as db:
            run = db.execute(
                'SELECT id FROM crawl_runs WHERE wallet=? AND source=? AND status=? '
                'ORDER BY id DESC LIMIT 1',
                (wallet, source, 'complete'),
            ).fetchone()
            if not run:
                return []
            if endpoint:
                rows = db.execute(
                    'SELECT * FROM raw_pages WHERE run_id=? AND endpoint=? ORDER BY id',
                    (run['id'], endpoint),
                ).fetchall()
            else:
                rows = db.execute(
                    'SELECT * FROM raw_pages WHERE run_id=? ORDER BY id',
                    (run['id'],),
                ).fetchall()
        return [
            {**dict(row), 'payload': decode_payload(row['payload']), 'params': json.loads(row['params'])}
            for row in rows
        ]

    def save_dashboard_snapshot(self, wallet: str, payload: dict):
        fetched_at = int(payload.get('fetched_at') or time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO dashboard_snapshots(wallet,fetched_at,payload) VALUES(?,?,?) '
                'ON CONFLICT(wallet) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload',
                (wallet, fetched_at, encode_payload(payload)),
            )

    def load_dashboard_snapshot(self, wallet: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT payload FROM dashboard_snapshots WHERE wallet=?',
                (wallet,),
            ).fetchone()
        return decode_payload(row['payload']) if row else None

    def list_dashboard_snapshots(self) -> List[dict]:
        """Return all current dashboard snapshots from the canonical database."""
        with self.connect() as db:
            rows = db.execute(
                'SELECT wallet,payload FROM dashboard_snapshots ORDER BY wallet',
            ).fetchall()
        return [decode_payload(row['payload']) for row in rows]

    def save_intraday_snapshot(self, wallet: str, payload: dict) -> bool:
        fetched_at = int(payload.get('fetched_at') or time.time())
        events = payload.get('events') or []
        timestamps = [int(item.get('timestamp') or 0) for item in events]
        positive_timestamps = [value for value in timestamps if value > 0]
        event_hash = hashlib.sha256(
            encode(sorted(events, key=self.event_id)).encode()
        ).hexdigest()
        status = str(payload.get('status') or 'unknown')
        coverage_days = int(payload.get('days') or 0)
        with self.connect() as db:
            watermark = db.execute(
                'SELECT payload_hash,coverage_days,status FROM wallet_component_watermarks '
                'WHERE wallet=? AND component="intraday"', (wallet,),
            ).fetchone()
            unchanged = bool(
                watermark
                and watermark['payload_hash'] == event_hash
                and int(watermark['coverage_days'] or 0) == coverage_days
                and str(watermark['status'] or '') == status
            )
            if unchanged:
                db.execute(
                    'UPDATE intraday_snapshots SET fetched_at=? WHERE wallet=?',
                    (fetched_at, wallet),
                )
            else:
                db.execute(
                    'INSERT INTO intraday_snapshots(wallet,fetched_at,payload) VALUES(?,?,?) '
                    'ON CONFLICT(wallet) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload',
                    (wallet, fetched_at, encode_payload(payload)),
                )
            self._save_component_watermark_tx(
                db, wallet, 'intraday',
                provider_cursor=payload.get('history_cursor'),
                oldest_timestamp=min(positive_timestamps, default=None),
                newest_timestamp=max(positive_timestamps, default=None),
                coverage_days=coverage_days,
                payload_hash=event_hash,
                status=status,
                updated_at=fetched_at,
            )
            self._mark_refresh_component_tx(
                db, wallet, 'intraday',
                status, timestamp=fetched_at,
            )
        return not unchanged

    def load_intraday_snapshot(self, wallet: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT payload FROM intraday_snapshots WHERE wallet=?',
                (wallet,),
            ).fetchone()
        return decode_payload(row['payload']) if row else None

    def save_activity_snapshot(
        self, wallet: str, payload: dict, event_delta: Optional[List[dict]] = None,
    ) -> bool:
        """Persist activity and optionally only the newly fetched event delta.

        The retained ``payload`` remains the complete analysis window.  A
        realtime overlap refresh can pass ``event_delta`` so the normalized
        event table does not re-upsert every historical event.  If the event
        content is unchanged, the large compressed payload is kept intact and
        only freshness/component watermarks advance.  The return value tells
        callers whether semantic event content changed.
        """
        fetched_at = int(payload.get('fetched_at') or time.time())
        start_at = int(payload.get('start_at') or 0)
        end_at = int(payload.get('end_at') or fetched_at)
        events = payload.get('events') or []
        now = int(time.time())
        timestamps = [int(item.get('timestamp') or 0) for item in events]
        positive_timestamps = [value for value in timestamps if value > 0]
        event_hash = hashlib.sha256(
            encode(sorted(events, key=self.event_id)).encode()
        ).hexdigest()
        persist_events = events if event_delta is None else (event_delta or [])
        status = str(payload.get('status') or 'unknown')
        coverage_days = int(payload.get('days') or 0)
        with self.connect() as db:
            watermark = db.execute(
                'SELECT payload_hash,coverage_days,status FROM wallet_component_watermarks '
                'WHERE wallet=? AND component="activity"', (wallet,),
            ).fetchone()
            # Databases created before activity watermarks existed can already
            # contain a complete JSON snapshot but no normalized event rows.
            # Populate the serving table from the full snapshot once during
            # the first post-migration refresh; subsequent overlap polls keep
            # the cheap event_delta path.
            if watermark is None and event_delta is not None:
                persist_events = events
            # Polling timestamps and provider cursors do not change the
            # activity facts used for labels. Keep the existing JSON blob and
            # event rows when its deterministic content hash is unchanged.
            unchanged = bool(
                watermark
                and watermark['payload_hash'] == event_hash
                and int(watermark['coverage_days'] or 0) == coverage_days
                and str(watermark['status'] or '') == status
            )
            event_values = []
            if not unchanged:
                for event in persist_events:
                    event_id = self.event_id(event)
                    body = encode(event)
                    timestamp = int(event.get('timestamp') or 0)
                    event_values.append(
                        (
                            wallet, event_id, timestamp,
                            str(event.get('event_type') or ''),
                            str(event.get('tx_hash') or ''),
                            str(event.get('token_address') or ''),
                            str(event.get('token_symbol') or ''),
                            self._finite_float(event.get('token_amount')),
                            self._finite_float(event.get('amount_usd')),
                            self._finite_float(event.get('buy_cost_usd')),
                            self._finite_float(event.get('price_usd')),
                            self._finite_float(event.get('quote_amount')),
                            str(event.get('quote_address') or ''),
                            self._finite_float(event.get('gas_usd')),
                            hashlib.sha256(body.encode()).hexdigest(),
                            now, now, '',
                        )
                    )
            if event_values:
                db.executemany(
                    'INSERT INTO wallet_events('
                    'wallet,event_id,timestamp,event_type,tx_hash,token_address,'
                    'token_symbol,token_amount,amount_usd,buy_cost_usd,price_usd,'
                    'quote_amount,quote_address,gas_usd,payload_hash,first_seen_at,last_seen_at,payload'
                    ') VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(wallet,event_id) DO UPDATE SET '
                    'last_seen_at=excluded.last_seen_at,payload_hash=excluded.payload_hash,'
                    'token_symbol=excluded.token_symbol,token_amount=excluded.token_amount,'
                    'amount_usd=excluded.amount_usd,buy_cost_usd=excluded.buy_cost_usd,'
                    'price_usd=excluded.price_usd,quote_amount=excluded.quote_amount,'
                    'quote_address=excluded.quote_address,gas_usd=excluded.gas_usd,'
                    'payload=excluded.payload '
                    'WHERE wallet_events.payload_hash IS NOT excluded.payload_hash',
                    event_values,
                )
            if unchanged:
                db.execute(
                    'UPDATE wallet_activity_snapshots SET fetched_at=? WHERE wallet=?',
                    (fetched_at, wallet),
                )
            else:
                db.execute(
                    'INSERT INTO wallet_activity_snapshots('
                    'wallet,fetched_at,start_at,end_at,payload) VALUES(?,?,?,?,?) '
                    'ON CONFLICT(wallet) DO UPDATE SET '
                    'fetched_at=excluded.fetched_at,start_at=excluded.start_at,'
                    'end_at=excluded.end_at,payload=excluded.payload',
                    (wallet, fetched_at, start_at, end_at, encode_payload(payload)),
                )
            self._save_component_watermark_tx(
                db, wallet, 'activity',
                provider_cursor=payload.get('history_cursor'),
                oldest_timestamp=min(positive_timestamps, default=None),
                newest_timestamp=max(positive_timestamps, default=None),
                coverage_days=coverage_days,
                history_exhausted=payload.get('history_exhausted') is True,
                payload_hash=event_hash,
                status=status,
                updated_at=fetched_at,
            )
            self._mark_refresh_component_tx(
                db, wallet, 'activity', status, timestamp=fetched_at,
            )
        return not unchanged

    @staticmethod
    def _finite_float(value):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result == result and abs(result) != float('inf') else None

    @staticmethod
    def event_id(event: dict) -> str:
        """Stable id used to make repeated provider pages idempotent."""
        key = (
            int(event.get('timestamp') or 0),
            str(event.get('tx_hash') or ''),
            str(event.get('event_type') or ''),
            str(event.get('token_address') or ''),
            str(event.get('token_amount') or ''),
            str(event.get('quote_address') or ''),
            str(event.get('quote_amount') or ''),
        )
        return hashlib.sha256(encode(key).encode()).hexdigest()

    @staticmethod
    def payload_hash(value) -> str:
        """Return a deterministic hash for a JSON-compatible component."""
        return hashlib.sha256(encode(value).encode()).hexdigest()

    def _save_component_watermark_tx(
        self, db, wallet: str, component: str, provider_cursor=None,
        oldest_timestamp=None, newest_timestamp=None, coverage_days=None,
        history_exhausted=False, payload_hash=None, status='unknown', updated_at=None,
    ) -> None:
        db.execute(
            'INSERT INTO wallet_component_watermarks('
            'wallet,component,provider_cursor,oldest_timestamp,newest_timestamp,'
            'coverage_days,history_exhausted,payload_hash,status,updated_at'
            ') VALUES(?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(wallet,component) DO UPDATE SET '
            'provider_cursor=excluded.provider_cursor,'
            'oldest_timestamp=excluded.oldest_timestamp,'
            'newest_timestamp=excluded.newest_timestamp,'
            'coverage_days=excluded.coverage_days,'
            'history_exhausted=excluded.history_exhausted,'
            'payload_hash=excluded.payload_hash,status=excluded.status,'
            'updated_at=excluded.updated_at',
            (
                wallet, component, provider_cursor, oldest_timestamp,
                newest_timestamp, coverage_days, int(bool(history_exhausted)),
                payload_hash, status,
                int(time.time() if updated_at is None else updated_at),
            ),
        )

    def save_component_watermark(self, wallet: str, component: str, **values) -> None:
        with self.connect() as db:
            self._save_component_watermark_tx(db, wallet, component, **values)

    def load_component_watermarks(self, wallet: str) -> Dict[str, dict]:
        with self.connect() as db:
            rows = db.execute(
                'SELECT * FROM wallet_component_watermarks WHERE wallet=?',
                (wallet,),
            ).fetchall()
        return {str(row['component']): dict(row) for row in rows}

    def enqueue_refresh(
        self, wallet: str, priority: int = 0, reason: str = '',
        requested_at: Optional[int] = None,
    ) -> None:
        timestamp = int(time.time() if requested_at is None else requested_at)
        with self.connect() as db:
            db.execute(
                'INSERT INTO wallet_refresh_queue(wallet,priority,requested_at,reason) '
                'VALUES(?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                'priority=MAX(wallet_refresh_queue.priority,excluded.priority),'
                'requested_at=MIN(wallet_refresh_queue.requested_at,excluded.requested_at),'
                'reason=CASE WHEN excluded.reason != "" THEN excluded.reason '
                'ELSE wallet_refresh_queue.reason END',
                (wallet, int(priority), timestamp, str(reason or '')),
            )

    def clear_refresh_request(self, wallet: str) -> None:
        """Remove a durable request after the direct worker has completed."""
        with self.connect() as db:
            db.execute('DELETE FROM wallet_refresh_queue WHERE wallet=?', (wallet,))

    def load_refresh_request(self, wallet: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT * FROM wallet_refresh_queue WHERE wallet=?', (wallet,),
            ).fetchone()
        return dict(row) if row else None

    def load_refresh_requests(self, wallets: Optional[List[str]] = None) -> Dict[str, dict]:
        """Load queued refresh metadata in one read for scheduler decisions."""
        if wallets is not None and not wallets:
            return {}
        addresses = list(dict.fromkeys(
            str(wallet).strip() for wallet in (wallets or []) if str(wallet).strip()
        ))
        if wallets is not None and not addresses:
            return {}
        with self.connect() as db:
            if not addresses:
                rows = db.execute(
                    'SELECT * FROM wallet_refresh_queue ORDER BY priority DESC, requested_at ASC',
                ).fetchall()
            else:
                result = []
                for offset in range(0, len(addresses), 500):
                    chunk = addresses[offset:offset + 500]
                    placeholders = ','.join('?' for _ in chunk)
                    result.extend(db.execute(
                        f'SELECT * FROM wallet_refresh_queue WHERE wallet IN ({placeholders})',
                        chunk,
                    ).fetchall())
                rows = result
        return {str(row['wallet']): dict(row) for row in rows}

    def claim_refresh_candidates(
        self, addresses: List[str], limit: int, now: Optional[int] = None,
        component_ttls: Optional[Dict[str, int]] = None,
        activity_days: int = 1095,
    ) -> List[str]:
        """Claim manual requests and only wallets with a stale component.

        When ``component_ttls`` is omitted this keeps the historical behavior
        used by migration/tests and returns the stalest addresses.  The live
        scheduler supplies per-component TTLs so a wallet whose inputs are all
        fresh is not selected merely because it is next in round-robin order.
        """
        addresses = list(dict.fromkeys(
            str(address).strip() for address in addresses if str(address).strip()
        ))
        if not addresses or limit <= 0:
            return []
        timestamp = int(time.time() if now is None else now)
        # The caller's identity list is authoritative.  Do not require a
        # wallet row to have been created by an earlier full crawl; a first
        # realtime/manual refresh must be claimable too.
        candidate_values = ','.join('( ? )' for _ in addresses)
        with self.connect() as db:
            due_clause = ''
            due_params = []
            if component_ttls:
                conditions = []
                component_aliases = {
                    'holdings': 'h', 'profits': 'p', 'calendar': 'cstate',
                    'stats': 's', 'activity': 'a', 'intraday': 'i',
                }
                for component, alias in component_aliases.items():
                    try:
                        ttl = max(1, int(component_ttls.get(component, 48 * 3600)))
                    except (TypeError, ValueError):
                        ttl = 48 * 3600
                    conditions.append(
                        f'({alias}.wallet IS NULL OR {alias}.status != "complete" '
                        f'OR {alias}.last_success_at IS NULL OR {alias}.last_success_at <= ?)'
                    )
                    due_params.append(timestamp - ttl)
                # A complete but short activity history must still be extended.
                conditions.append(
                    '(a.wallet IS NULL OR a.status != "complete" '
                    'OR (COALESCE(aw.coverage_days,0) < ? '
                    'AND COALESCE(aw.history_exhausted,0)=0))'
                )
                try:
                    required_days = max(1, int(activity_days))
                except (TypeError, ValueError):
                    required_days = 1095
                due_params.append(required_days)
                due_clause = (
                    "AND ((COALESCE(q.priority,0)>=100 OR q.reason='user_request') OR "
                    + ' OR '.join(conditions) + ') '
                )
            else:
                due_clause = ''
            rows = db.execute(
                f'WITH candidates(address) AS (VALUES {candidate_values}) '
                'SELECT c.address FROM candidates c '
                'LEFT JOIN dashboard_snapshots d ON d.wallet=c.address '
                'LEFT JOIN wallet_refresh_state r ON r.wallet=c.address '
                'LEFT JOIN wallet_refresh_queue q ON q.wallet=c.address '
                'LEFT JOIN wallet_refresh_components h ON h.wallet=c.address AND h.component="holdings" '
                'LEFT JOIN wallet_refresh_components p ON p.wallet=c.address AND p.component="profits" '
                'LEFT JOIN wallet_refresh_components cstate ON cstate.wallet=c.address AND cstate.component="calendar" '
                'LEFT JOIN wallet_refresh_components s ON s.wallet=c.address AND s.component="stats" '
                'LEFT JOIN wallet_refresh_components a ON a.wallet=c.address AND a.component="activity" '
                'LEFT JOIN wallet_refresh_components i ON i.wallet=c.address AND i.component="intraday" '
                'LEFT JOIN wallet_component_watermarks aw ON aw.wallet=c.address AND aw.component="activity" '
                # A high-priority user request intentionally bypasses the
                # provider retry cooldown; timer requests remain cooled down.
                "WHERE (COALESCE(r.next_retry_at,0)<=? OR COALESCE(q.priority,0)>=100 "
                "OR q.reason='user_request') "
                + due_clause +
                'ORDER BY COALESCE(q.priority,0) DESC,'
                'COALESCE(q.requested_at,9223372036854775807) ASC,'
                'COALESCE(r.last_success_at,d.fetched_at,0),c.address LIMIT ?',
                (*addresses, timestamp, *due_params, int(limit)),
            ).fetchall()
            selected = [str(row['address']) for row in rows]
            if selected:
                selected_placeholders = ','.join('?' for _ in selected)
                db.execute(
                    f'DELETE FROM wallet_refresh_queue WHERE wallet IN ({selected_placeholders})',
                    selected,
                )
        return selected

    def load_activity_snapshot(self, wallet: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT payload FROM wallet_activity_snapshots WHERE wallet=?',
                (wallet,),
            ).fetchone()
        return decode_payload(row['payload']) if row else None

    def load_activity_resume_cursor(self, wallet: str) -> Optional[str]:
        """Return the last page cursor so extending history cannot skip boundary rows."""
        with self.connect() as db:
            row = db.execute(
                'SELECT rp.params FROM raw_pages rp '
                'JOIN crawl_runs cr ON cr.id=rp.run_id '
                'AND cr.status IN ("complete","partial") '
                'WHERE rp.wallet=? AND rp.endpoint="portfolio/activity" '
                'AND rp.source="gmgn_activity" '
                'ORDER BY rp.id DESC LIMIT 1',
                (wallet,),
            ).fetchone()
        if not row:
            return None
        try:
            return (json.loads(row['params']) or {}).get('cursor')
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def load_stats_snapshot(self, wallet: str, period: Optional[str] = '30d') -> Optional[dict]:
        with self.connect() as db:
            row = None
            if period:
                row = db.execute(
                    'SELECT rp.payload,rp.fetched_at FROM raw_pages rp '
                    'JOIN crawl_runs cr ON cr.id=rp.run_id AND cr.status="complete" '
                    'WHERE rp.wallet=? AND rp.endpoint="portfolio/stats" '
                    'AND rp.params LIKE ? ORDER BY rp.fetched_at DESC LIMIT 1',
                    (wallet, f'%"{period}"%'),
                ).fetchone()
            # Do not fall back to a different period.  A 7d payload presented as
            # 30d silently changes rank semantics and is worse than missing data.
            if not row and period is None:
                row = db.execute(
                    'SELECT rp.payload,rp.fetched_at FROM raw_pages rp '
                    'JOIN crawl_runs cr ON cr.id=rp.run_id AND cr.status="complete" '
                    'WHERE rp.wallet=? AND rp.endpoint="portfolio/stats" '
                    'ORDER BY rp.fetched_at DESC LIMIT 1',
                    (wallet,),
                ).fetchone()
        if not row:
            return None
        payload = decode_payload(row['payload'])
        if isinstance(payload, dict):
            payload['_snapshot_fetched_at'] = int(row['fetched_at'])
            if period:
                payload['_snapshot_period'] = period
        return payload

    def load_stats_period_snapshots(self, wallet: str) -> Dict[str, dict]:
        """Return the newest stored GMGN stats payload for each supported period."""
        with self.connect() as db:
            rows = db.execute(
                'SELECT rp.params,rp.payload,rp.fetched_at FROM raw_pages rp '
                'JOIN crawl_runs cr ON cr.id=rp.run_id AND cr.status="complete" '
                'WHERE rp.wallet=? AND rp.endpoint="portfolio/stats" '
                'ORDER BY rp.fetched_at DESC',
                (wallet,),
            ).fetchall()
        result: Dict[str, dict] = {}
        for row in rows:
            try:
                period = str((json.loads(row['params']) or {}).get('period') or '')
                payload = decode_payload(row['payload'])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if period in ('7d', '30d') and period not in result and isinstance(payload, dict):
                payload['_snapshot_fetched_at'] = int(row['fetched_at'])
                result[period] = payload
        return result

    def save_external_snapshot(
        self, provider: str, kind: str, wallet: str, payload: dict,
        status: str = 'complete',
    ) -> None:
        fetched_at = int(payload.get('fetched_at') or time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO external_data_snapshots('
                'provider,kind,wallet,fetched_at,status,payload) VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(provider,kind,wallet) DO UPDATE SET '
                'fetched_at=excluded.fetched_at,status=excluded.status,payload=excluded.payload',
                (provider, kind, wallet, fetched_at, status, encode_payload(payload)),
            )

    def load_external_snapshot(
        self, provider: str, kind: str, wallet: str,
    ) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT payload FROM external_data_snapshots '
                'WHERE provider=? AND kind=? AND wallet=?',
                (provider, kind, wallet),
            ).fetchone()
        return decode_payload(row['payload']) if row else None

    def save_onchain_tx(self, signature: str, payload: dict) -> None:
        fetched_at = int(time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO onchain_tx_cache(signature, fetched_at, payload) VALUES(?,?,?) '
                'ON CONFLICT(signature) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload',
                (signature, fetched_at, encode_payload(payload)),
            )

    def load_onchain_tx(self, signature: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT payload FROM onchain_tx_cache WHERE signature=?',
                (signature,),
            ).fetchone()
        return decode_payload(row['payload']) if row else None

    def load_onchain_txs(self, signatures: List[str]) -> Dict[str, dict]:
        if not signatures:
            return {}
        result = {}
        # Batch in chunks of 500 for SQLite parameter limits
        with self.connect() as db:
            for offset in range(0, len(signatures), 500):
                chunk = signatures[offset:offset + 500]
                placeholders = ','.join('?' for _ in chunk)
                rows = db.execute(
                    f'SELECT signature, payload FROM onchain_tx_cache WHERE signature IN ({placeholders})',
                    chunk,
                ).fetchall()
                for r in rows:
                    result[r['signature']] = decode_payload(r['payload'])
        return result


    def save_intraday_job(
        self,
        wallet: str,
        status: str,
        days: int,
        pages: int = 0,
        events: int = 0,
        error: Optional[str] = None,
    ):
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO intraday_backfill_jobs('
                'wallet,status,days,pages,events,started_at,finished_at,error'
                ') VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(wallet) DO UPDATE SET '
                'status=excluded.status,days=excluded.days,pages=excluded.pages,'
                'events=excluded.events,'
                "started_at=CASE WHEN excluded.status='running' THEN excluded.started_at "
                'ELSE intraday_backfill_jobs.started_at END,'
                'finished_at=excluded.finished_at,error=excluded.error',
                (
                    wallet,
                    status,
                    days,
                    pages,
                    events,
                    now if status == 'running' else None,
                    now if status in ('complete', 'failed') else None,
                    error,
                ),
            )

    def load_intraday_job(self, wallet: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT * FROM intraday_backfill_jobs WHERE wallet=?',
                (wallet,),
            ).fetchone()
        return dict(row) if row else None

    def select_refresh_candidates(
        self, addresses: List[str], limit: int, now: Optional[int] = None,
    ) -> List[str]:
        """Select the stalest eligible wallets while respecting retry cooldowns."""
        if not addresses or limit <= 0:
            return []
        timestamp = int(time.time() if now is None else now)
        placeholders = ','.join('?' for _ in addresses)
        with self.connect() as db:
            rows = db.execute(
                'SELECT w.address FROM wallets w '
                'LEFT JOIN dashboard_snapshots d ON d.wallet=w.address '
                'LEFT JOIN wallet_refresh_state r ON r.wallet=w.address '
                f'WHERE w.address IN ({placeholders}) '
                'AND COALESCE(r.next_retry_at,0)<=? '
                'ORDER BY COALESCE(r.last_success_at,d.fetched_at,0),w.address LIMIT ?',
                (*addresses, timestamp, int(limit)),
            ).fetchall()
        return [str(row['address']) for row in rows]

    def mark_refresh_attempt(self, wallet: str, attempted_at: Optional[int] = None) -> None:
        timestamp = int(attempted_at or time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO wallet_refresh_state(wallet,status,last_attempt_at) '
                'VALUES(?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                'status=excluded.status,last_attempt_at=excluded.last_attempt_at,error=NULL',
                (wallet, 'running', timestamp),
            )

    def mark_refresh_success(self, wallet: str, completed_at: Optional[int] = None) -> None:
        timestamp = int(completed_at or time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO wallet_refresh_state('
                'wallet,status,last_attempt_at,last_success_at,consecutive_failures,next_retry_at,error'
                ') VALUES(?,?,?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                'status=excluded.status,last_success_at=excluded.last_success_at,'
                'consecutive_failures=0,next_retry_at=0,error=NULL',
                (wallet, 'complete', timestamp, timestamp, 0, 0, None),
            )

    def mark_refresh_failure(
        self, wallet: str, error: str, retry_after_seconds: int = 900,
        completed_at: Optional[int] = None,
    ) -> None:
        timestamp = int(completed_at or time.time())
        with self.connect() as db:
            current = db.execute(
                'SELECT consecutive_failures FROM wallet_refresh_state WHERE wallet=?',
                (wallet,),
            ).fetchone()
            failures = int(current['consecutive_failures'] if current else 0) + 1
            cooldown = min(21600, max(60, int(retry_after_seconds)) * (2 ** min(failures - 1, 4)))
            db.execute(
                'INSERT INTO wallet_refresh_state('
                'wallet,status,last_attempt_at,consecutive_failures,next_retry_at,error'
                ') VALUES(?,?,?,?,?,?) ON CONFLICT(wallet) DO UPDATE SET '
                'status=excluded.status,consecutive_failures=excluded.consecutive_failures,'
                'next_retry_at=excluded.next_retry_at,error=excluded.error',
                (wallet, 'failed', timestamp, failures, timestamp + cooldown, str(error)[:2000]),
            )

    def load_refresh_state(self, wallet: str) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT * FROM wallet_refresh_state WHERE wallet=?', (wallet,),
            ).fetchone()
        return dict(row) if row else None

    def mark_refresh_component(
        self, wallet: str, component: str, status: str,
        error: Optional[str] = None, timestamp: Optional[int] = None,
    ) -> None:
        with self.connect() as db:
            self._mark_refresh_component_tx(
                db, wallet, component, status, error=error, timestamp=timestamp,
            )

    @staticmethod
    def _mark_refresh_component_tx(
        db, wallet: str, component: str, status: str,
        error: Optional[str] = None, timestamp: Optional[int] = None,
    ) -> None:
        now = int(time.time() if timestamp is None else timestamp)
        success_at = now if status == 'complete' else None
        db.execute(
            'INSERT INTO wallet_refresh_components('
            'wallet,component,status,last_attempt_at,last_success_at,error'
            ') VALUES(?,?,?,?,?,?) ON CONFLICT(wallet,component) DO UPDATE SET '
            'status=excluded.status,last_attempt_at=excluded.last_attempt_at,'
            'last_success_at=COALESCE(excluded.last_success_at,'
            'wallet_refresh_components.last_success_at),error=excluded.error',
            (wallet, component, status, now, success_at, str(error)[:2000] if error else None),
        )

    def load_refresh_components(self, wallet: str) -> Dict[str, dict]:
        with self.connect() as db:
            rows = db.execute(
                'SELECT * FROM wallet_refresh_components WHERE wallet=?', (wallet,),
            ).fetchall()
        return {str(row['component']): dict(row) for row in rows}

    def save_feature_run(
        self,
        wallet: str,
        feature_version: str,
        input_hash: str,
        payload: dict,
    ) -> int:
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO wallet_feature_runs('
                'wallet,feature_version,input_hash,computed_at,payload'
                ') VALUES(?,?,?,?,?) ON CONFLICT(wallet,feature_version,input_hash) '
                'DO UPDATE SET computed_at=excluded.computed_at,payload=excluded.payload',
                (wallet, feature_version, input_hash, now, encode_payload(payload)),
            )
            row = db.execute(
                'SELECT id FROM wallet_feature_runs '
                'WHERE wallet=? AND feature_version=? AND input_hash=?',
                (wallet, feature_version, input_hash),
            ).fetchone()
        return int(row['id'])

    def save_label_run(
        self,
        wallet: str,
        formula_version: str,
        input_hash: str,
        feature_run_id: int,
        payload: dict,
    ) -> int:
        now = int(time.time())
        with self.connect() as db:
            db.execute(
                'INSERT INTO wallet_label_runs('
                'wallet,formula_version,input_hash,feature_run_id,computed_at,payload'
                ') VALUES(?,?,?,?,?,?) ON CONFLICT(wallet,formula_version,input_hash) '
                'DO UPDATE SET feature_run_id=excluded.feature_run_id,'
                'computed_at=excluded.computed_at,payload=excluded.payload',
                (
                    wallet,
                    formula_version,
                    input_hash,
                    feature_run_id,
                    now,
                    encode_payload(payload),
                ),
            )
            row = db.execute(
                'SELECT id FROM wallet_label_runs '
                'WHERE wallet=? AND formula_version=? AND input_hash=?',
                (wallet, formula_version, input_hash),
            ).fetchone()
        return int(row['id'])

    def load_label_run(
        self,
        wallet: str,
        formula_version: str,
        input_hash: str,
    ) -> Optional[dict]:
        with self.connect() as db:
            row = db.execute(
                'SELECT id,feature_run_id,computed_at,payload FROM wallet_label_runs '
                'WHERE wallet=? AND formula_version=? AND input_hash=?',
                (wallet, formula_version, input_hash),
            ).fetchone()
        if not row:
            return None
        return {
            'id': row['id'],
            'feature_run_id': row['feature_run_id'],
            'computed_at': row['computed_at'],
            'payload': decode_payload(row['payload']),
        }

    def compact_payloads(self, vacuum: bool = False, batch_size: int = 100) -> dict:
        """Compress existing JSON payloads in-place without discarding provenance."""
        tables = (
            'raw_pages',
            'token_rows',
            'wallet_summaries',
            'dashboard_snapshots',
            'intraday_snapshots',
            'wallet_activity_snapshots',
            'wallet_events',
            'wallet_feature_runs',
            'wallet_label_runs',
            'external_data_snapshots',
            'onchain_tx_cache',
        )
        result = {'rows_scanned': 0, 'rows_compressed': 0, 'by_table': {}}
        with self.connect() as db:
            for table in tables:
                last_rowid = 0
                scanned = 0
                compressed = 0
                while True:
                    rows = db.execute(
                        f'SELECT rowid AS payload_rowid,payload FROM {table} '
                        'WHERE typeof(payload)="text" AND rowid>? '
                        'ORDER BY rowid LIMIT ?',
                        (last_rowid, batch_size),
                    ).fetchall()
                    if not rows:
                        break
                    last_rowid = int(rows[-1]['payload_rowid'])
                    updates = []
                    for row in rows:
                        scanned += 1
                        value = encode_payload(json.loads(row['payload']))
                        if isinstance(value, (bytes, bytearray, memoryview)):
                            updates.append((value, row['payload_rowid']))
                    if updates:
                        db.executemany(
                            f'UPDATE {table} SET payload=? WHERE rowid=?',
                            updates,
                        )
                        compressed += len(updates)
                    db.commit()
                result['by_table'][table] = {
                    'rows_scanned': scanned,
                    'rows_compressed': compressed,
                }
                result['rows_scanned'] += scanned
                result['rows_compressed'] += compressed
        if vacuum:
            with self.connect() as db:
                db.execute('VACUUM')
        return result

    def prune_raw_pages(
        self,
        keep_days: int = 90,
        failed_days: int = 14,
        dry_run: bool = True,
        now: Optional[int] = None,
    ) -> dict:
        """Prune old raw provider pages while retaining recent audit evidence.

        Complete runs are retained for ``keep_days``. Pages belonging to
        failed/partial runs are useful for a shorter troubleshooting window,
        so they use ``failed_days``. Running runs are never removed. The
        default is a dry run because deleting raw evidence is irreversible;
        callers must explicitly pass ``dry_run=False`` before deleting.
        """
        keep_days = int(keep_days)
        failed_days = int(failed_days)
        if keep_days < 1 or failed_days < 1:
            raise ValueError('keep_days and failed_days must be positive')
        timestamp = int(time.time() if now is None else now)
        complete_cutoff = timestamp - keep_days * 86400
        failed_cutoff = timestamp - failed_days * 86400
        result = {
            'dry_run': bool(dry_run),
            'keep_days': keep_days,
            'failed_days': failed_days,
            'complete_cutoff': complete_cutoff,
            'failed_cutoff': failed_cutoff,
            'rows_candidates': 0,
            'rows_deleted': 0,
            'by_status': {},
        }
        predicate = (
            '(cr.status="complete" AND COALESCE(cr.ended_at,cr.started_at) < ?) '
            'OR (cr.status IN ("failed","partial") '
            'AND COALESCE(cr.ended_at,cr.started_at) < ?)'
        )
        with self.connect() as db:
            rows = db.execute(
                'SELECT cr.status,COUNT(*) AS count FROM raw_pages rp '
                'JOIN crawl_runs cr ON cr.id=rp.run_id '
                f'WHERE {predicate} GROUP BY cr.status',
                (complete_cutoff, failed_cutoff),
            ).fetchall()
            result['by_status'] = {
                str(row['status']): int(row['count']) for row in rows
            }
            result['rows_candidates'] = sum(result['by_status'].values())
            if not dry_run and result['rows_candidates']:
                cursor = db.execute(
                    'DELETE FROM raw_pages WHERE id IN ('
                    'SELECT rp.id FROM raw_pages rp '
                    'JOIN crawl_runs cr ON cr.id=rp.run_id '
                    f'WHERE {predicate})',
                    (complete_cutoff, failed_cutoff),
                )
                result['rows_deleted'] = int(cursor.rowcount)
        return result
