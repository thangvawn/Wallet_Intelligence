"""FastAPI backend for the deterministic GMGN PnL and wallet-rank pipeline."""
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.api import (
    cohort_stats,
    load_cohort_label_index,
    load_master_index,
    load_wallet_pnl,
    lookup_wallet,
    wallet_stats,
)
from app.portfolio_pnl.intraday import load_intraday_report
from app.runtime import BASE, load_settings
from app.wallet_identity import load_message_wallets


load_settings()
backfill_executor = ThreadPoolExecutor(max_workers=1)
backfill_lock = threading.Lock()
backfill_job = {'status': 'pending'}
wallet_refresh_lock = threading.Lock()
wallet_refresh_jobs = {}
continuous_refresh_lock = threading.Lock()
continuous_refresh_job = {'status': 'pending'}
continuous_refresh_stop = threading.Event()
continuous_refresh_thread = None


@asynccontextmanager
async def lifespan(_app):
    start_missing_data_backfill()
    start_continuous_database_refresh()
    try:
        yield
    finally:
        continuous_refresh_stop.set()


app = FastAPI(
    title='CT Wallet Rank Engine',
    description='Deterministic GMGN history analysis and S/A/B/C/D wallet labels',
    version='5.0.0',
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


class AutoLabelRequest(BaseModel):
    wallet_address: str
    wallet_name: str = ''
    max_pages: int = Field(200, ge=1, le=1000)
    hide_closed: bool = False
    # Labels still read SQLite immediately; true explicitly requests a TTL
    # override for the queued provider refresh.
    refresh: bool = False
    activity_days: int = Field(1095, ge=1, le=3650)


def schedule_wallet_database_refresh(
    wallet_address: str, wallet_name: str = '', max_pages: int = 200,
    activity_days: int = 1095, force: bool = False,
):
    """Queue provider collection without changing the label returned now.

    Normal label requests remain due-only. ``force`` is reserved for a caller
    that explicitly asks for an immediate provider refresh (the request model's
    ``refresh=true`` flag).
    """
    address = wallet_address.strip()
    now = int(time.time())
    with wallet_refresh_lock:
        existing = wallet_refresh_jobs.get(address) or {}
        if existing.get('status') in ('queued', 'running'):
            return dict(existing)
        if (
            existing.get('status') == 'complete'
            and not force
            and now - int(existing.get('completed_at') or 0) < 900
        ):
            return dict(existing)
        wallet_refresh_jobs[address] = {
            'wallet_address': address,
            'status': 'queued',
            'queued_at': now,
            'force': bool(force),
        }

    # Persist the request so a periodic batch cannot starve an explicit user
    # refresh. The direct worker removes it after the attempt completes.
    from app.portfolio_pnl.store import PnlStore
    try:
        PnlStore().enqueue_refresh(address, priority=100, reason='user_request')
    except Exception as exc:
        with wallet_refresh_lock:
            wallet_refresh_jobs[address].update({
                'status': 'failed', 'completed_at': int(time.time()),
                'error': str(exc),
            })
        raise

    def work():
        from app.portfolio_pnl.component_refresh import refresh_wallet_components
        from app.portfolio_pnl.store import PnlStore

        store = PnlStore()
        with wallet_refresh_lock:
            wallet_refresh_jobs[address].update({
                'status': 'running', 'started_at': int(time.time()),
            })
        store.mark_refresh_attempt(address)
        try:
            result = refresh_wallet_components(
                address,
                wallet_name=wallet_name,
                holdings_max_pages=max_pages,
                activity_days=activity_days,
                store=store,
                component_ttls=_component_refresh_ttls(),
                # ``force`` is true only for an explicit refresh=true request;
                # ordinary label requests stay due-only and do not spend
                # provider quota on fresh wallets.
                force=force,
            )
            if result.get('status') != 'complete':
                failures = result.get('failed_components') or []
                detail = '; '.join(
                    f"{item.get('component')}: {item.get('error')}"
                    for item in failures
                ) or f"Còn thiếu: {', '.join(result.get('remaining_components') or [])}"
                from app.portfolio_pnl.canonical_data import sync_canonical_wallet
                result['canonical'] = sync_canonical_wallet(store, address, error=detail)
                store.mark_refresh_failure(address, detail)
                with wallet_refresh_lock:
                    wallet_refresh_jobs[address].update({
                        'status': 'partial',
                        'completed_at': int(time.time()),
                        'result': result,
                        'error': detail,
                    })
                return
            from app.portfolio_pnl.canonical_data import load_canonical_summary
            changed_components = result.get('changed_components') or []
            if 'changed_components' not in result or changed_components:
                from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
                analyze_wallet_from_database(address, store=store)
            # Analysis already projects the report once. Return only the
            # compact manifest from the refresh worker instead of writing and
            # loading all token facts a second time.
            result['canonical'] = load_canonical_summary(store, address)
            result['changed_components'] = changed_components
            store.mark_refresh_success(address)
            with wallet_refresh_lock:
                wallet_refresh_jobs[address].update({
                    'status': 'complete',
                    'completed_at': int(time.time()),
                    'result': result,
                })
        except Exception as exc:
            try:
                from app.portfolio_pnl.canonical_data import sync_canonical_wallet
                sync_canonical_wallet(store, address, error=str(exc))
            except Exception:
                pass
            store.mark_refresh_failure(address, str(exc))
            with wallet_refresh_lock:
                wallet_refresh_jobs[address].update({
                    'status': 'failed',
                    'completed_at': int(time.time()),
                    'error': str(exc),
                })
        finally:
            # The queue entry is only a durable hand-off marker for the
            # periodic worker.  Remove it after the direct worker has finished
            # so a concurrent scheduler cannot lose the explicit request, but
            # also cannot execute the same request a second time later.
            store.clear_refresh_request(address)

    # Share one worker with startup backfill so GMGN requests remain serialized.
    backfill_executor.submit(work)
    with wallet_refresh_lock:
        return dict(wallet_refresh_jobs[address])


def start_missing_data_backfill():
    """Fill missing snapshots and new dates; never recrawl complete history."""
    if os.getenv('CT_WALLET_SKIP_STARTUP_BACKFILL') == '1':
        with backfill_lock:
            backfill_job.clear()
            backfill_job.update({'status': 'disabled', 'reason': 'environment_override'})
        return
    from app.portfolio_pnl.latest_export import backfill_missing_wallets

    with backfill_lock:
        backfill_job.clear()
        backfill_job.update({
            'status': 'running', 'processed': 0, 'missing': None,
            'incremental': None, 'filled': 0, 'incremental_filled': 0,
            'fill_failed': 0,
        })

    def update_progress(update):
        with backfill_lock:
            backfill_job.update(update)

    def work():
        try:
            result = backfill_missing_wallets(progress=update_progress)
            from app.portfolio_pnl.enrichment import collect_missing_data
            from app.portfolio_pnl.latest_export import export_from_database

            enrichment = collect_missing_data(
                components=('stats', 'intraday', 'empty_holdings', 'activity'),
                activity_days=365,
                progress=lambda state: update_progress({
                    'phase': 'enrichment',
                    'enrichment': state,
                }),
            )
            latest_export = export_from_database()
            with backfill_lock:
                backfill_job.clear()
                backfill_job.update({
                    **result,
                    'status': (
                        'complete'
                        if result.get('status') == 'complete'
                        and enrichment.get('status') == 'complete'
                        and latest_export.get('status') == 'complete'
                        else 'partial'
                    ),
                    'phase': 'complete',
                    'enrichment': enrichment,
                    'exports': latest_export.get('exports'),
                    'labels_ready': latest_export.get('labels_ready'),
                })
        except Exception as exc:
            with backfill_lock:
                backfill_job.clear()
                backfill_job.update({'status': 'failed', 'error': str(exc)})

    backfill_executor.submit(work)


def _positive_env_int(name: str, default: int, maximum: int) -> int:
    try:
        return min(maximum, max(1, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def _component_refresh_ttls() -> dict:
    """Configurable clocks let cheap realtime inputs refresh more often."""
    defaults = {
        'holdings': 6 * 3600,
        'profits': 15 * 60,
        'calendar': 15 * 60,
        'stats': 30 * 60,
        'activity': 10 * 60,
        'intraday': 10 * 60,
    }
    return {
        component: _positive_env_int(
            f'CT_WALLET_TTL_{component.upper()}_SECONDS', default, 7 * 86400,
        )
        for component, default in defaults.items()
    }


def start_continuous_database_refresh():
    """Continuously queue small, serialized refresh batches for the stalest wallets."""
    global continuous_refresh_thread
    if os.getenv('CT_WALLET_AUTO_REFRESH', '1') == '0':
        with continuous_refresh_lock:
            continuous_refresh_job.clear()
            continuous_refresh_job.update({
                'status': 'disabled', 'reason': 'CT_WALLET_AUTO_REFRESH=0',
            })
        return
    if continuous_refresh_thread and continuous_refresh_thread.is_alive():
        return

    batch_size = _positive_env_int('CT_WALLET_REFRESH_BATCH_SIZE', 4, 50)
    interval = _positive_env_int('CT_WALLET_REFRESH_INTERVAL_SECONDS', 120, 86400)
    initial_delay = _positive_env_int('CT_WALLET_REFRESH_INITIAL_DELAY_SECONDS', 60, 3600)
    wallet_delay = _positive_env_int('CT_WALLET_REFRESH_WALLET_DELAY_SECONDS', 5, 300)
    component_ttls = _component_refresh_ttls()
    continuous_refresh_stop.clear()
    with continuous_refresh_lock:
        continuous_refresh_job.clear()
        continuous_refresh_job.update({
            'status': 'scheduled',
            'batch_size': batch_size,
            'interval_seconds': interval,
            'wallet_delay_seconds': wallet_delay,
            'component_ttls': component_ttls,
            'next_run_at': int(time.time()) + initial_delay,
        })

    def loop():
        delay = initial_delay
        while not continuous_refresh_stop.wait(delay):
            with backfill_lock:
                startup_running = backfill_job.get('status') == 'running'
            if startup_running:
                with continuous_refresh_lock:
                    continuous_refresh_job.update({
                        'status': 'waiting_for_startup_backfill',
                        'next_run_at': int(time.time()) + 60,
                    })
                delay = 60
                continue

            with continuous_refresh_lock:
                continuous_refresh_job.update({
                    'status': 'queued', 'queued_at': int(time.time()),
                })

            def work():
                from app.portfolio_pnl.continuous_refresh import run_refresh_batch

                with continuous_refresh_lock:
                    continuous_refresh_job.update({
                        'status': 'running', 'started_at': int(time.time()),
                    })
                try:
                    result = run_refresh_batch(
                        batch_size=batch_size,
                        wallet_delay_seconds=wallet_delay,
                        component_ttls=component_ttls,
                    )
                    with continuous_refresh_lock:
                        continuous_refresh_job.update({
                            **result,
                            'status': result.get('status', 'complete'),
                            'completed_at': int(time.time()),
                        })
                except Exception as exc:
                    with continuous_refresh_lock:
                        continuous_refresh_job.update({
                            'status': 'failed',
                            'completed_at': int(time.time()),
                            'error': str(exc),
                        })

            # The same single-worker executor serializes startup, manual and periodic refreshes.
            future = backfill_executor.submit(work)
            future.result()
            delay = interval
            with continuous_refresh_lock:
                continuous_refresh_job['next_run_at'] = int(time.time()) + delay

    continuous_refresh_thread = threading.Thread(
        target=loop, name='wallet-database-refresh-scheduler', daemon=True,
    )
    continuous_refresh_thread.start()


@app.get('/api/health')
def health_check():
    return {
        'status': 'ok',
        'version': '5.0.0',
        'wallets_count': len(load_message_wallets()),
        'provider': {
            'gmgn_api_key': bool(os.getenv('GMGN_API_KEY')),
            'gmgn_cli': bool(shutil.which('gmgn-cli')),
        },
    }


@app.get('/api/pnl/index')
def pnl_index():
    data = load_master_index()
    if not data.get('wallets'):
        raise HTTPException(status_code=404, detail='Chưa có dữ liệu PnL.')
    return data


@app.get('/api/pnl/lookup/{wallet_address}')
def pnl_lookup(wallet_address: str):
    """Read a wallet from the current local snapshot without refreshing it."""
    try:
        return lookup_wallet(wallet_address)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'Không đọc được PnL: {exc}')


@app.get('/api/pnl/wallets/{wallet_address}')
def pnl_wallet(wallet_address: str):
    payload = load_wallet_pnl(wallet_address.strip())
    if not payload:
        raise HTTPException(status_code=404, detail='Chưa có snapshot cho ví này.')
    return payload


@app.get('/api/pnl/stats')
def pnl_cohort_stats(include_wallets: bool = Query(False)):
    data = cohort_stats(include_wallets=include_wallets)
    if not data.get('wallets_with_payload'):
        raise HTTPException(status_code=404, detail='Chưa có dữ liệu PnL.')
    return data


@app.get('/api/pnl/stats/{wallet_address}')
def pnl_wallet_stats(wallet_address: str):
    stats = wallet_stats(wallet_address)
    if not stats:
        raise HTTPException(status_code=404, detail='Không tìm thấy PnL cho ví này.')
    return stats


@app.get('/api/pnl/intraday/{wallet_address}')
def pnl_intraday_stats(
    wallet_address: str,
    refresh: bool = Query(False),
    days: int = Query(365, ge=30, le=365),
):
    try:
        return load_intraday_report(
            wallet_address.strip(), days=days, tz_offset_hours=7, refresh=refresh,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Không tải được thống kê theo giờ: {exc}')


@app.get('/api/pnl/analysis/{wallet_address}')
def pnl_database_analysis(wallet_address: str):
    try:
        return analyze_wallet_from_database(wallet_address)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'Phân tích database thất bại: {exc}')


@app.get('/api/pnl/canonical/{wallet_address}')
def pnl_canonical_wallet(
    wallet_address: str,
    include_tokens: bool = Query(True),
):
    """Return the continuously refreshed typed canonical data for one wallet."""
    from app.portfolio_pnl.canonical_data import load_canonical_wallet
    from app.portfolio_pnl.store import PnlStore

    result = load_canonical_wallet(PnlStore(), wallet_address.strip(), include_tokens=include_tokens)
    if result is None:
        raise HTTPException(status_code=404, detail='Wallet chưa có canonical data.')
    return {'schema': 'canonical_wallet_output_v1', 'wallet': result}


@app.get('/api/pnl/canonical')
def pnl_canonical_index():
    """Return coverage/status for the complete canonical wallet cohort."""
    from app.portfolio_pnl.canonical_data import list_canonical_wallet_state
    from app.portfolio_pnl.store import PnlStore

    wallets = list_canonical_wallet_state(PnlStore())
    return {
        'schema': 'canonical_wallet_index_v1',
        'total_wallets': len(wallets),
        'ready_wallets': sum(item.get('data_status') == 'READY' for item in wallets),
        'wallets': wallets,
    }


@app.get('/api/pnl/cohort-labels')
def pnl_cohort_labels():
    """Return one compact, label-ready row for every canonical wallet."""
    return load_cohort_label_index()


@app.post('/api/pnl/analyze-and-label')
def pnl_analyze_and_label(req: AutoLabelRequest):
    """Label the current SQLite snapshot, then queue a database-only refresh."""
    from app.portfolio_pnl.analytics import build_wallet_report
    from app.portfolio_pnl.single_wallet_pipeline import run_single_wallet_pipeline
    from app.portfolio_pnl.store import PnlStore

    try:
        result = run_single_wallet_pipeline(
            req.wallet_address,
            wallet_name=req.wallet_name,
            refresh=False,
            holdings_max_pages=req.max_pages,
            activity_days=req.activity_days,
        )
        snapshot = PnlStore().load_dashboard_snapshot(req.wallet_address.strip())
        result['dashboard'] = build_wallet_report(snapshot or {})
        result['database_update'] = schedule_wallet_database_refresh(
            req.wallet_address,
            wallet_name=req.wallet_name,
            max_pages=req.max_pages,
            activity_days=req.activity_days,
            force=req.refresh,
        )
        return result
    except FileNotFoundError as exc:
        job = schedule_wallet_database_refresh(
            req.wallet_address,
            wallet_name=req.wallet_name,
            max_pages=req.max_pages,
            activity_days=req.activity_days,
            force=req.refresh,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f'{exc} Đã xếp lịch cập nhật database ở chế độ nền '
                f"({job.get('status')}). Hãy thử xuất label lại sau."
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f'Không thể phân tích và ghi label: {exc}')


@app.get('/api/pnl/refresh/status/{wallet_address}')
def wallet_database_refresh_status(wallet_address: str):
    from app.portfolio_pnl.store import PnlStore

    address = wallet_address.strip()
    with wallet_refresh_lock:
        runtime = dict(wallet_refresh_jobs.get(address) or {
            'wallet_address': address,
            'status': 'not_scheduled',
        })
    store = PnlStore()
    runtime['persistent_state'] = store.load_refresh_state(address)
    runtime['components'] = store.load_refresh_components(address)
    runtime['watermarks'] = store.load_component_watermarks(address)
    runtime['queued_request'] = store.load_refresh_request(address)
    return runtime


@app.get('/api/pnl/auto-refresh/status')
def continuous_database_refresh_status():
    with continuous_refresh_lock:
        return dict(continuous_refresh_job)


@app.get('/api/pnl/backfill/status')
def missing_data_backfill_status():
    with backfill_lock:
        return dict(backfill_job)


@app.get('/api/pnl/exports/download')
def download_current_label_export(format: Literal['json', 'csv'] = 'json'):
    """Recalculate labels from SQLite, without refreshing wallet history."""
    from app.portfolio_pnl.auto_label import DEFAULT_LABEL_JSON
    from app.portfolio_pnl.latest_export import export_from_database

    with backfill_lock:
        status = backfill_job.get('status')
    if status == 'running':
        raise HTTPException(status_code=409, detail='Server đang bổ sung data thiếu; vui lòng đợi.')
    result = export_from_database()
    if not result.get('labels_ready'):
        raise HTTPException(status_code=404, detail='Chưa có snapshot để xuất label.')
    path = DEFAULT_LABEL_JSON if format == 'json' else DEFAULT_LABEL_JSON.with_suffix('.csv')
    if not path.exists():
        raise HTTPException(status_code=404, detail='Không tìm thấy file export.')
    media_type = 'application/json' if format == 'json' else 'text/csv'
    return Response(
        content=path.read_bytes(),
        media_type=media_type,
        headers={'Content-Disposition': f'attachment; filename={path.name}'},
    )


PNL_WEB_DIR = Path(BASE) / 'web' / 'pnl'
if PNL_WEB_DIR.exists():
    app.mount('/', StaticFiles(directory=str(PNL_WEB_DIR), html=True), name='static')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app.server:app', host='127.0.0.1', port=8081, reload=True)
