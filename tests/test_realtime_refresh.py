import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.portfolio_pnl.component_refresh import (
    _save_dashboard_component,
    components_needing_refresh,
    refresh_wallet_components,
)
from app.portfolio_pnl.store import PnlStore


class RealtimeRefreshTests(unittest.TestCase):
    def make_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return PnlStore(Path(directory.name) / 'runtime.sqlite3')

    def seed_complete_wallet(self, store, wallet='wallet', now=None):
        now = int(now or time.time())
        store.add_wallet(wallet)
        store.save_dashboard_snapshot(wallet, {
            'wallet_address': wallet,
            'fetched_at': now,
            'component_fetched_at': {
                'holdings': now, 'profits': now, 'calendar': now,
            },
            'rows': [{'token_address': 'token'}],
            'summary': {'total_profit_usd': 1},
            'daily_calendar': [{'date': '2026-09-28', 'realized_pnl_usd': 1}],
            'collection': {'complete': True},
        })
        run_id = store.start_run(wallet, 'seed', {})
        store.append_page(
            run_id, wallet, 'seed', 'portfolio/stats',
            {'period': '7d'}, {'winrate': 0.5}, 1,
        )
        store.append_page(
            run_id, wallet, 'seed', 'portfolio/stats',
            {'period': '30d'}, {'winrate': 0.6}, 1,
        )
        store.finish_run(run_id, 'complete', rows=2)
        store.save_activity_snapshot(wallet, {
            'wallet_address': wallet,
            'fetched_at': now,
            'start_at': now - 1095 * 86400,
            'end_at': now,
            'days': 1095,
            'status': 'complete',
            'events': [{'timestamp': now, 'event_type': 'sell'}],
        })
        store.save_intraday_snapshot(wallet, {
            'wallet_address': wallet,
            'fetched_at': now,
            'status': 'complete',
            'events': [],
        })
        return now

    def test_append_pages_commits_batch_and_updates_run_counters(self):
        store = self.make_store()
        run_id = store.start_run('wallet', 'gmgn', {})
        pages = [
            {'params': {'cursor': None}, 'payload': {'data': [1]}, 'count': 1},
            {'params': {'cursor': 'next'}, 'payload': {'data': [2, 3]}, 'count': 2},
        ]

        self.assertEqual(
            store.append_pages(run_id, 'wallet', 'gmgn', 'portfolio/holdings', pages),
            2,
        )
        with store.connect() as db:
            run = db.execute(
                'SELECT pages,rows FROM crawl_runs WHERE id=?', (run_id,),
            ).fetchone()
            stored = db.execute(
                'SELECT COUNT(*) FROM raw_pages WHERE run_id=?', (run_id,),
            ).fetchone()[0]
        self.assertEqual((run['pages'], run['rows'], stored), (2, 3, 2))

    def test_component_ttls_are_independent(self):
        store = self.make_store()
        now = self.seed_complete_wallet(store, now=1_000_000)
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component(
                'wallet', component, 'complete', timestamp=now,
            )

        needed = components_needing_refresh(
            store, 'wallet', now=now + 120,
            component_ttls={
                'holdings': 3600,
                'profits': 60,
                'calendar': 3600,
                'stats': 3600,
                'activity': 3600,
                'intraday': 3600,
            },
        )
        self.assertEqual(needed, ['profits'])

    def test_identical_component_does_not_overwrite_snapshot(self):
        store = self.make_store()
        self.seed_complete_wallet(store)
        current = store.load_dashboard_snapshot('wallet')

        changed = _save_dashboard_component(
            store, 'wallet', 'profits',
            {'summary': current['summary']},
        )

        self.assertFalse(changed)
        self.assertEqual(store.load_dashboard_snapshot('wallet'), current)

    def test_stats_period_never_falls_back_to_another_window(self):
        store = self.make_store()
        run_id = store.start_run('wallet', 'seed', {})
        store.append_page(
            run_id, 'wallet', 'seed', 'portfolio/stats',
            {'period': '7d'}, {'winrate': 0.7}, 1,
        )
        store.finish_run(run_id, 'complete', rows=1)

        self.assertIsNone(store.load_stats_snapshot('wallet', period='30d'))
        self.assertEqual(store.load_stats_snapshot('wallet', period='7d')['winrate'], 0.7)

    def test_stats_reader_ignores_pages_from_failed_run(self):
        store = self.make_store()
        failed_run = store.start_run('wallet', 'gmgn', {})
        store.append_page(
            failed_run, 'wallet', 'gmgn', 'portfolio/stats',
            {'period': '30d'}, {'winrate': 0.99}, 1,
        )
        store.finish_run(failed_run, 'failed', error='429')
        self.assertIsNone(store.load_stats_snapshot('wallet', period='30d'))

        complete_run = store.start_run('wallet', 'gmgn', {})
        store.append_page(
            complete_run, 'wallet', 'gmgn', 'portfolio/stats',
            {'period': '30d'}, {'winrate': 0.50}, 1,
        )
        store.finish_run(complete_run, 'complete', rows=1)
        self.assertEqual(
            store.load_stats_snapshot('wallet', period='30d')['winrate'], 0.50,
        )

    def test_activity_events_are_idempotent_and_watermarked(self):
        store = self.make_store()
        payload = {
            'wallet_address': 'wallet',
            'fetched_at': 1_000_100,
            'days': 30,
            'history_cursor': 'cursor-1',
            'status': 'complete',
            'events': [{
                'timestamp': 1_000_000,
                'event_type': 'sell',
                'tx_hash': 'tx-1',
                'token_address': 'token-1',
                'amount_usd': 12.5,
            }],
        }
        store.save_activity_snapshot('wallet', payload)
        store.save_activity_snapshot('wallet', payload)

        with store.connect() as db:
            count = db.execute(
                'SELECT COUNT(*) FROM wallet_events WHERE wallet=?', ('wallet',),
            ).fetchone()[0]
        self.assertEqual(count, 1)
        watermark = store.load_component_watermarks('wallet')['activity']
        self.assertEqual(watermark['provider_cursor'], 'cursor-1')
        self.assertEqual(watermark['oldest_timestamp'], 1_000_000)
        self.assertEqual(watermark['newest_timestamp'], 1_000_000)
        self.assertEqual(watermark['coverage_days'], 30)
        self.assertEqual(
            store.load_refresh_components('wallet')['activity']['status'],
            'complete',
        )

    def test_refresh_queue_prioritizes_manual_requests_and_claims_once(self):
        store = self.make_store()
        store.enqueue_refresh('old', priority=1, requested_at=10, reason='timer')
        store.enqueue_refresh('manual', priority=100, requested_at=20, reason='user_request')

        selected = store.claim_refresh_candidates(
            ['old', 'manual', 'never_crawled'], limit=2, now=30,
        )
        self.assertEqual(selected, ['manual', 'old'])
        self.assertEqual(
            store.claim_refresh_candidates(
                ['old', 'manual', 'never_crawled'], limit=3, now=30,
            ),
            ['manual', 'never_crawled', 'old'],
        )

    def test_due_candidate_filter_skips_fresh_wallet_but_manual_bypasses_ttl(self):
        store = self.make_store()
        now = self.seed_complete_wallet(store, now=1_000_000)
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component('wallet', component, 'complete', timestamp=now)

        ttls = {component: 3600 for component in (
            'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
        )}
        self.assertEqual(
            store.claim_refresh_candidates(
                ['wallet'], limit=1, now=now + 60,
                component_ttls=ttls, activity_days=1095,
            ),
            [],
        )

        # A user-requested refresh is always eligible, even when all component
        # clocks are inside their normal TTL.
        store.enqueue_refresh('wallet', priority=100, requested_at=now + 60, reason='user_request')
        self.assertEqual(
            store.claim_refresh_candidates(
                ['wallet'], limit=1, now=now + 60,
                component_ttls=ttls, activity_days=1095,
            ),
            ['wallet'],
        )

    def test_due_candidate_filter_selects_stale_component(self):
        store = self.make_store()
        now = self.seed_complete_wallet(store, now=1_000_000)
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component('wallet', component, 'complete', timestamp=now)
        store.mark_refresh_component('wallet', 'profits', 'complete', timestamp=now - 5000)
        ttls = {component: 3600 for component in (
            'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
        )}
        self.assertEqual(
            store.claim_refresh_candidates(
                ['wallet'], limit=1, now=now,
                component_ttls=ttls, activity_days=1095,
            ),
            ['wallet'],
        )

    def test_refresh_batch_does_not_call_provider_for_fresh_wallet(self):
        from app.portfolio_pnl.continuous_refresh import run_refresh_batch

        store = self.make_store()
        now = self.seed_complete_wallet(store, now=int(time.time()))
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component('wallet', component, 'complete', timestamp=now)
        ttls = {component: 3600 for component in (
            'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
        )}
        calls = []

        def should_not_run(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError('fresh wallet must not enter the refresher')

        result = run_refresh_batch(
            store=store,
            identities=[{'address': 'wallet', 'name': 'Test wallet'}],
            batch_size=1,
            wallet_delay_seconds=0,
            activity_days=1095,
            component_ttls=ttls,
            refresher=should_not_run,
        )
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['selected'], 0)
        self.assertEqual(calls, [])

    def test_explicit_force_refresh_bypasses_component_ttls(self):
        store = self.make_store()
        now = self.seed_complete_wallet(store, now=1_000_000)
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component('wallet', component, 'complete', timestamp=now)
        self.assertEqual(
            components_needing_refresh(
                store, 'wallet', now=now + 60,
                component_ttls={component: 3600 for component in (
                    'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
                )},
                force=True,
            ),
            ['holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'],
        )

    def test_refresh_batch_does_not_reanalyze_when_payload_is_unchanged(self):
        from app.portfolio_pnl.continuous_refresh import run_refresh_batch

        store = self.make_store()
        now = self.seed_complete_wallet(store, now=int(time.time()))
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component('wallet', component, 'complete', timestamp=now)
        store.mark_refresh_component('wallet', 'profits', 'complete', timestamp=now - 5000)
        ttls = {component: 3600 for component in (
            'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
        )}

        def unchanged_refresher(*args, **kwargs):
            return {
                'status': 'complete', 'updated_at': int(time.time()),
                'completed_components': ['profits'],
                'changed_components': [], 'remaining_components': [],
            }

        with patch('app.portfolio_pnl.continuous_refresh.analyze_wallet_from_database') as analyze:
            result = run_refresh_batch(
                store=store,
                identities=[{'address': 'wallet', 'name': 'Test wallet'}],
                batch_size=1,
                wallet_delay_seconds=0,
                activity_days=1095,
                component_ttls=ttls,
                refresher=unchanged_refresher,
            )
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['completed'][0]['changed_components'], [])
        analyze.assert_not_called()

    def test_queued_manual_request_reaches_refresher_with_force(self):
        from app.portfolio_pnl.continuous_refresh import run_refresh_batch

        store = self.make_store()
        now = self.seed_complete_wallet(store, now=int(time.time()))
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component('wallet', component, 'complete', timestamp=now)
        store.mark_refresh_failure('wallet', 'previous 429', retry_after_seconds=3600)
        store.enqueue_refresh('wallet', priority=100, reason='user_request', requested_at=now)
        ttls = {component: 3600 for component in (
            'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
        )}
        calls = []

        def manual_refresher(*args, **kwargs):
            calls.append(kwargs)
            return {
                'status': 'complete', 'updated_at': int(time.time()),
                'completed_components': [], 'changed_components': [],
                'remaining_components': [],
            }

        result = run_refresh_batch(
            store=store,
            identities=[{'address': 'wallet', 'name': 'Test wallet'}],
            batch_size=1,
            wallet_delay_seconds=0,
            activity_days=1095,
            component_ttls=ttls,
            refresher=manual_refresher,
        )
        self.assertEqual(result['selected'], 1)
        self.assertTrue(calls[0]['force'])

    def test_prune_raw_pages_is_dry_run_by_default_and_preserves_recent(self):
        store = self.make_store()
        old_run = store.start_run('wallet', 'gmgn', {})
        store.append_page(
            old_run, 'wallet', 'gmgn', 'portfolio/holdings', {}, {'old': True}, 1,
        )
        with store.connect() as db:
            db.execute(
                'UPDATE crawl_runs SET started_at=?,ended_at=?,status=? WHERE id=?',
                (1, 1, 'complete', old_run),
            )
        recent_run = store.start_run('wallet', 'gmgn', {})
        store.append_page(
            recent_run, 'wallet', 'gmgn', 'portfolio/holdings', {}, {'new': True}, 1,
        )
        store.finish_run(recent_run, 'complete', rows=1)

        preview = store.prune_raw_pages(
            keep_days=1, failed_days=1, dry_run=True, now=86400 * 3,
        )
        self.assertEqual(preview['rows_candidates'], 1)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM raw_pages').fetchone()[0], 2)

        result = store.prune_raw_pages(
            keep_days=1, failed_days=1, dry_run=False, now=86400 * 3,
        )
        self.assertEqual(result['rows_deleted'], 1)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM raw_pages').fetchone()[0], 1)

    def test_component_failure_does_not_rollback_later_success(self):
        store = self.make_store()
        now = self.seed_complete_wallet(store)
        for component in ('holdings', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component(
                'wallet', component, 'complete', timestamp=now,
            )
        for component in ('profits', 'calendar'):
            store.mark_refresh_component(
                'wallet', component, 'complete', timestamp=now - 1000,
            )

        class Client:
            def fetch_wallet_profits(self, wallet, period):
                raise RuntimeError('profits unavailable')

            def fetch_daily_profit_calendar(self, wallet, days=365):
                return ([{
                    'date': '2026-09-29', 'realized_pnl_usd': 2,
                }], {'code': 0, 'data': {'list': []}})

        with patch('app.portfolio_pnl.component_refresh.time.time', return_value=now):
            result = refresh_wallet_components(
                'wallet', store=store, client=Client(),
                component_ttls={
                    'holdings': 3600, 'profits': 60, 'calendar': 60,
                    'stats': 3600, 'activity': 3600, 'intraday': 3600,
                },
            )

        self.assertEqual(result['status'], 'partial')
        self.assertIn('calendar', result['completed_components'])
        self.assertEqual(result['failed_components'][0]['component'], 'profits')
        dates = {
            row['date']
            for row in store.load_dashboard_snapshot('wallet')['daily_calendar']
        }
        self.assertEqual(dates, {'2026-09-28', '2026-09-29'})

    def test_empty_complete_activity_window_is_not_refetched_forever(self):
        store = self.make_store()
        now = int(time.time())
        store.save_activity_snapshot('wallet', {
            'wallet_address': 'wallet', 'fetched_at': now,
            'days': 1095, 'history_exhausted': True,
            'status': 'complete', 'events': [],
        })
        store.mark_refresh_component('wallet', 'activity', 'complete', timestamp=now)
        self.assertNotIn(
            'activity',
            components_needing_refresh(store, 'wallet', now=now + 10),
        )

    def test_due_candidate_filter_respects_exhausted_activity_history(self):
        store = self.make_store()
        now = int(time.time())
        store.save_activity_snapshot('wallet', {
            'wallet_address': 'wallet', 'fetched_at': now,
            'days': 30, 'history_exhausted': True,
            'status': 'complete', 'events': [],
        })
        for component in ('holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday'):
            store.mark_refresh_component('wallet', component, 'complete', timestamp=now)
        ttls = {component: 3600 for component in (
            'holdings', 'profits', 'calendar', 'stats', 'activity', 'intraday',
        )}
        self.assertEqual(
            store.claim_refresh_candidates(
                ['wallet'], limit=1, now=now + 60,
                component_ttls=ttls, activity_days=1095,
            ),
            [],
        )

    def test_activity_resume_cursor_ignores_failed_run_pages(self):
        store = self.make_store()
        failed = store.start_run('wallet', 'gmgn_activity', {})
        store.append_page(
            failed, 'wallet', 'gmgn_activity', 'portfolio/activity',
            {'cursor': 'unsafe'}, {'data': []}, 0,
        )
        store.finish_run(failed, 'failed', error='429')
        self.assertIsNone(store.load_activity_resume_cursor('wallet'))

        complete = store.start_run('wallet', 'gmgn_activity', {})
        store.append_page(
            complete, 'wallet', 'gmgn_activity', 'portfolio/activity',
            {'cursor': 'safe'}, {'data': []}, 0,
        )
        store.finish_run(complete, 'complete')
        self.assertEqual(store.load_activity_resume_cursor('wallet'), 'safe')


if __name__ == '__main__':
    unittest.main()
