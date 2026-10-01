"""One-shot GMGN collection -> statistical label -> CSV/JSON export."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.runtime import load_settings, valid_address
from app.wallet_identity import load_message_wallets
from app.portfolio_pnl.analysis_pipeline import analyze_wallet_from_database
from app.portfolio_pnl.gmgn_client import GmgnPnlClient
from app.portfolio_pnl.label_export import upsert_label_exports
from app.portfolio_pnl.label_projection import project_wallet_label
from app.portfolio_pnl.service import crawl_wallet
from app.portfolio_pnl.store import BASE, PnlStore


# Keep generated cohort exports inside the deployable engine directory.  This
# avoids a runtime dependency on a parent checkout when the engine is copied
# to a server as a standalone application.
DEFAULT_LABEL_JSON = BASE / 'data' / 'ct_wallet_alpha_labels.json'


def analyze_and_label_wallet(
    wallet_address: str,
    wallet_name: str = '',
    max_pages: int = 200,
    hide_closed: bool = False,
    output_json: Path = DEFAULT_LABEL_JSON,
    store: PnlStore = None,
    client: GmgnPnlClient = None,
) -> Dict[str, Any]:
    """Run the full deterministic workflow for one Solana wallet."""
    address = wallet_address.strip()
    identity = next(
        (item for item in load_message_wallets() if item['address'] == address),
        {},
    )
    name = wallet_name.strip() or identity.get('name', '')
    emoji = identity.get('emoji', '')
    if not valid_address(address):
        raise ValueError('Địa chỉ Solana phải giải mã base58 thành 32 byte.')
    if not 1 <= max_pages <= 1000:
        raise ValueError('max_pages phải nằm trong khoảng 1–1000.')

    load_settings()
    pnl_store = store or PnlStore()
    gmgn_client = client or GmgnPnlClient()
    crawl = crawl_wallet(
        pnl_store,
        gmgn_client,
        address,
        name,
        max_pages=max_pages,
        hide_closed=hide_closed,
    )
    if crawl.get('status') != 'ok':
        raise RuntimeError(crawl.get('error') or 'Không thu thập được dữ liệu GMGN.')

    analysis = analyze_wallet_from_database(address, store=pnl_store)
    label = project_wallet_label(analysis, name=name, emoji=emoji)
    # This legacy one-shot route does not collect activity/intraday history;
    # expose that limitation instead of returning a misleading READY label.
    from app.portfolio_pnl.single_wallet_pipeline import _data_availability
    label['data_availability'] = _data_availability(
        pnl_store, address, analysis, refreshed=True,
        as_of=datetime.now(tz=timezone.utc), required_activity_days=1095,
    )
    if label['data_availability']['status'] != 'READY':
        label['labels']['rank_status'] = 'PROVISIONAL'
    exports = upsert_label_exports(label, Path(output_json))
    return {
        'status': 'ok',
        'wallet_address': address,
        'formula_version': analysis.get('pipeline', {}).get('formula_version'),
        'input_hash': analysis.get('pipeline', {}).get('input_hash'),
        'collection': crawl.get('collection') or {},
        'exports': exports,
        'label': label,
    }
