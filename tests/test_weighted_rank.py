import copy
import unittest

from app.portfolio_pnl.rank_engine import RANK_WEIGHTS, evaluate_rank


def report(**overrides):
    values = {
        'pnl': 250_000,
        'roi': 25,
        'jackpot': 25,
        'drawdown_usd': 10_000,
        'drawdown_pct': 10,
        'longest_loss': 5,
        'current_loss': 2,
        'outflow': 10,
        'first_sell': 25,
        'averaging_down': 10,
        'days_since': 0,
    }
    values.update(overrides)
    return {
        'activity_profile': {'days_since_last_active': values['days_since']},
        'performance_profile': {'all_time': {
            'total_profit_usd': values['pnl'], 'roi_on_cost_pct': values['roi'],
        }},
        'payoff_profile': {
            'top1_gross_profit_share_pct': values['jackpot'],
            'max_realized_pnl_drawdown_usd': values['drawdown_usd'],
            'longest_loss_streak': values['longest_loss'],
            'current_loss_streak': values['current_loss'],
            'median_outflow_holding_pct': values['outflow'],
        },
        'risk_profile': {
            'max_realized_pnl_drawdown_usd': values['drawdown_usd'],
            'realized_pnl_drawdown_to_peak_pnl_pct': values['drawdown_pct'],
        },
        'group_labels': {'g2': {'conviction_metrics': {'averaging_down': values['averaging_down']}},
                         'g4': {'median_first_sell_pct': values['first_sell']}},
    }


class WeightedRankTests(unittest.TestCase):
    def test_weights_sum_to_one_hundred_and_score_is_exposed(self):
        self.assertEqual(sum(RANK_WEIGHTS.values()), 100)
        decision = evaluate_rank(report())
        self.assertEqual(decision['rank'], 'S')
        self.assertEqual(decision['rule'], 'S_WEIGHTED_SCORE')
        self.assertEqual(decision['rank_confidence'], 1.0)
        self.assertEqual(set(decision['rank_score_components']), set(RANK_WEIGHTS))

    def test_missing_fields_reduce_confidence_without_zeroing_score(self):
        decision = evaluate_rank(report(jackpot=None, outflow=None, first_sell=None, averaging_down=None))
        self.assertEqual(decision['rank'], 'A')
        self.assertEqual(decision['rank_status'], 'CONFIRMED')
        self.assertEqual(decision['available_rank_weight'], 68.0)
        self.assertIsNotNone(decision['overall_score'])
        self.assertIn('jackpot_dependency_pct', decision['missing_rank_metrics'])

    def test_risk_veto_precedes_high_aggregate_score(self):
        decision = evaluate_rank(report(current_loss=8))
        self.assertEqual(decision['rank'], 'D')
        self.assertEqual(decision['rule'], 'D3_CURRENT_LOSS_STREAK')
        self.assertIn('D3_CURRENT_LOSS_STREAK', decision['risk_vetoes'])

    def test_inactivity_is_not_a_rank_veto(self):
        decision = evaluate_rank(report(days_since=147))
        self.assertEqual(decision['rank'], 'S')
        self.assertEqual(decision['rule'], 'S_WEIGHTED_SCORE')
        self.assertNotIn('D1_INACTIVE', decision['risk_vetoes'])
        self.assertNotIn('D1_INACTIVE', decision['s_gates'])

    def test_legacy_drawdown_field_is_ignored(self):
        baseline = evaluate_rank(report())
        changed = copy.deepcopy(report())
        changed['risk_profile']['max_drawdown_to_peak_pct'] = 999999
        self.assertEqual(evaluate_rank(changed), baseline)
        self.assertNotIn('drawdown_control', str(baseline['s_gates']))


if __name__ == '__main__':
    unittest.main()
