import unittest

from app.portfolio_pnl.drawdown_engine import reconstruct_nav, ReconstructionError


def point(t, balances, prices, **kwargs):
    return dict(timestamp=t, sequence=0, balances=balances, prices=prices, **kwargs)


class NavDrawdownTests(unittest.TestCase):
    def calculate(self, rows):
        return reconstruct_nav(rows, ledger_reconciled=True)

    def test_withdrawal_is_not_loss(self):
        rows = [point(1, {'SOL': 10}, {'SOL': 100}),
                point(2, {'SOL': 10}, {'SOL': 150}),
                point(3, {'SOL': 6}, {'SOL': 150}, before_balances={'SOL': 10},
                      external_quantities={'SOL': -4}),
                point(4, {'SOL': 6}, {'SOL': 120})]
        self.assertEqual(self.calculate(rows)['portfolio_max_drawdown_pct'], 20)

    def test_token_loss_before_transfer_is_preserved(self):
        rows = [point(1, {'USD': 400, 'A': 6}, {'USD': 1, 'A': 100}),
                point(2, {'USD': 400}, {'USD': 1, 'A': 50},
                      before_balances={'USD': 400, 'A': 6}, external_quantities={'A': -6}),
                point(3, {'USD': 400}, {'USD': 1})]
        self.assertEqual(self.calculate(rows)['portfolio_max_drawdown_pct'], 30)

    def test_deposit_does_not_erase_drawdown(self):
        rows = [point(1, {'A': 10}, {'A': 100}),
                point(2, {'A': 20}, {'A': 80}, before_balances={'A': 10},
                      external_quantities={'A': 10})]
        self.assertEqual(self.calculate(rows)['portfolio_max_drawdown_pct'], 20)

    def test_fee_remains_loss(self):
        rows = [point(1, {'USD': 1000}, {'USD': 1}),
                point(2, {'USD': 490}, {'USD': 1}, before_balances={'USD': 1000},
                      external_quantities={'USD': -500})]
        self.assertEqual(self.calculate(rows)['portfolio_max_drawdown_pct'], 2)

    def test_empty_wallet_starts_new_segment(self):
        rows = [point(1, {'USD': 1000}, {'USD': 1}),
                point(2, {}, {'USD': 1}, before_balances={'USD': 1000}, external_quantities={'USD': -1000}),
                point(3, {'USD': 100}, {'USD': 1}, before_balances={}, external_quantities={'USD': 100}),
                point(4, {'USD': 90}, {'USD': 1})]
        result = self.calculate(rows)
        self.assertEqual(result['segment_count'], 2)
        self.assertEqual(result['portfolio_max_drawdown_pct'], 10)

    def test_missing_prices_and_unreconciled_ledger_fail(self):
        rows = [point(1, {'A': 10}, {'A': 10}), point(2, {'A': 10}, {})]
        with self.assertRaisesRegex(ReconstructionError, 'missing_price'):
            self.calculate(rows)
        with self.assertRaisesRegex(ReconstructionError, 'ledger_not_reconciled'):
            reconstruct_nav(rows)

    def test_unrealized_loss_and_total_loss(self):
        rows = [point(1, {'A': 10}, {'A': 100}), point(2, {'A': 10}, {'A': 0})]
        self.assertEqual(self.calculate(rows)['portfolio_max_drawdown_pct'], 100)

    def test_duplicate_timestamps_require_sequence(self):
        rows = [point(1, {'USD': 100}, {'USD': 1}), point(1, {'USD': 100}, {'USD': 1})]
        with self.assertRaisesRegex(ReconstructionError, 'duplicate'):
            self.calculate(rows)


if __name__ == '__main__':
    unittest.main()
