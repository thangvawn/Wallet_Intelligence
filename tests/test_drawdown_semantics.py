import copy
import unittest

from app.portfolio_pnl.wallet_labels import build_pnl_wallet_label
from app.portfolio_pnl.label_projection import project_wallet_label
from app.portfolio_pnl.rank_engine import evaluate_rank


def report_for(profits):
    daily = [
        {'date': f'2026-01-{i:02d}', 'realized_pnl_usd': value,
         'profit_usd': max(value, 0), 'loss_usd': max(-value, 0),
         'source': 'gmgn_web_daily_profits'}
        for i, value in enumerate(profits, 1)
    ]
    return build_pnl_wallet_label(
        'test-wallet', payload_override={'summary': {}, 'daily_calendar': daily},
        activity_events_override=[],
    )


class DrawdownSemanticsTests(unittest.TestCase):
    def test_pnl_drawdown_is_not_portfolio_mdd(self):
        report = report_for([10, -100])
        risk = report['risk_profile']
        self.assertEqual(risk['max_realized_pnl_drawdown_usd'], 100)
        self.assertEqual(risk['realized_pnl_drawdown_to_peak_pnl_pct'], 1000)
        metrics = project_wallet_label(report)['metrics']
        self.assertIsNone(metrics['portfolio_max_drawdown_pct'])
        self.assertEqual(metrics['portfolio_drawdown_status'], 'NOT_RECONSTRUCTED')
        self.assertFalse(metrics['drawdown_used_for_ranking'])

    def test_no_positive_peak_and_missing_history_remain_unknown(self):
        self.assertIsNone(report_for([-100])['risk_profile']['realized_pnl_drawdown_to_peak_pnl_pct'])
        self.assertIsNone(report_for([])['risk_profile']['max_realized_pnl_drawdown_usd'])

    def test_score_does_not_depend_on_drawdown_path(self):
        early_gain = report_for([10, -100])['information_value']
        early_loss = report_for([-100, 10])['information_value']
        self.assertNotIn('drawdown_control', early_gain['score_components'])
        self.assertNotIn('drawdown_control', early_gain['score_component_sources'])
        self.assertEqual(early_gain['composite_alpha_score'], early_loss['composite_alpha_score'])

    def test_rank_gates_ignore_legacy_drawdown(self):
        report = report_for([10, -100])
        baseline = evaluate_rank(report)
        for value in [None, 0, 60, 100, 871930.7]:
            changed = copy.deepcopy(report)
            changed['risk_profile']['max_drawdown_to_peak_pct'] = value
            self.assertEqual(evaluate_rank(changed), baseline)
        self.assertNotIn('drawdown_control', str(baseline['s_gates']))


if __name__ == '__main__':
    unittest.main()
