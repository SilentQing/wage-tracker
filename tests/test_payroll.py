import copy
from test_overtime import LedgerTestCase


class PayrollTests(LedgerTestCase):
    def record(self, quantity=10, price='2.50'):
        response = self.client.post('/api/records', json=dict(record_date='2024-02-10', part_name='轴套', quantity=quantity, unit_price=price))
        self.assertEqual(response.status_code, 200)
        return response.json()['id']

    def reject(self, record, quantity):
        return self.client.put(f'/api/records/{record}/rejection', json=dict(quantity=quantity, reason='尺寸不合格'))

    def pay(self, value='10.00', key='payment-00000001', month='2024-02'):
        return self.client.post('/api/payments', json=dict(id=key, month=month, paid_date='2024-03-10', amount=value, note='工资'))

    def bonus(self, awarded=True):
        return self.client.put('/api/attendance-bonus', json=dict(month='2024-02', awarded=awarded))

    def test_rejection_preserves_submission_and_price_snapshot(self):
        record = self.record()
        self.assertEqual(self.reject(record, 2).status_code, 200)
        self.client.post('/api/price', json=dict(part_name='轴套', unit_price=99))
        row = self.data()['records'][0]
        self.assertEqual((row['quantity'], row['rejected_quantity'], row['payable_quantity'], row['amount']), (10, 2, 8, 20))
        self.assertEqual(self.data()['totals']['rejection_deduction'], 5)
        self.assertEqual(self.reject(record, 11).status_code, 400)
        response = self.client.put(f'/api/records/{record}', json=dict(record_date='2024-02-10', part_name='轴套', quantity=1))
        self.assertEqual(response.status_code, 400)
        self.client.delete(f'/api/records/{record}')
        self.assertEqual(self.data()['totals']['piece_wage'], 0)
        self.client.post(f'/api/records/{record}/restore')
        self.assertEqual(self.data()['totals']['piece_wage'], 20)
        self.reject(record, 0)
        self.assertEqual(self.data()['totals']['piece_wage'], 25)

    def test_rounding_bonus_and_multiple_payments(self):
        record = self.record(quantity=3, price='0.335')
        self.reject(record, 1)
        self.bonus()
        self.assertEqual(self.data()['totals']['total_pay'], 100.67)
        self.assertEqual(self.data()['reconciliation']['status'], '未到账')
        self.pay('50')
        self.assertEqual(self.data()['reconciliation']['status'], '部分到账')
        self.pay('50.67', 'payment-00000002')
        info = self.data()['reconciliation']
        self.assertEqual((info['paid'], info['difference'], info['status']), (100.67, 0, '已结清'))
        self.assertEqual(self.data('2024-03')['reconciliation']['paid'], 0)
        self.assertEqual(self.data('ALL')['reconciliation']['paid'], 100.67)
        self.bonus(False)
        self.assertEqual(self.data()['reconciliation']['status'], '超额到账')

    def test_lock_blocks_earnings_but_accepts_payments_and_voids(self):
        record = self.record()
        self.bonus()
        self.client.put('/api/month-lock', json=dict(month='2024-02', locked=True))
        self.assertEqual(self.reject(record, 1).status_code, 409)
        self.assertEqual(self.bonus(False).status_code, 409)
        self.assertEqual(self.pay().status_code, 200)
        self.assertEqual(self.client.delete('/api/payments/payment-00000001').status_code, 200)
        self.assertEqual(self.data()['reconciliation']['paid'], 0)
        self.assertEqual(self.data()['payments'][0]['voided'], 1)
        self.pay()
        self.assertEqual(self.data()['reconciliation']['paid'], 0)

    def test_retry_deduplication_and_input_validation(self):
        self.pay()
        self.pay()
        self.assertEqual(len(self.data()['payments']), 1)
        self.assertEqual(self.pay('11').status_code, 409)
        self.assertEqual(self.pay('0', 'payment-00000002').status_code, 422)
        self.assertEqual(self.pay('1.001', 'payment-00000002').status_code, 422)
        self.assertEqual(self.pay('1', 'payment-00000002', 'ALL').status_code, 400)
        self.assertEqual(self.client.put('/api/attendance-bonus', json=dict(month='2099-01', awarded=True)).status_code, 400)
        response = self.client.post('/api/payments', json=dict(id='payment-00000003', month='2024-02', paid_date='2099-01-01', amount=1))
        self.assertEqual(response.status_code, 400)

    def test_backup_restore_references_rollback_and_legacy(self):
        record = self.record()
        self.reject(record, 2)
        self.bonus()
        self.pay()
        backup = self.client.get('/api/backup').json()
        self.reject(record, 8)
        self.assertEqual(self.client.post('/api/restore', json=backup).status_code, 200)
        self.assertEqual(self.data()['totals']['total_pay'], 120)
        self.assertEqual(self.data()['reconciliation']['paid'], 10)
        broken = copy.deepcopy(backup)
        broken['tables']['rejections'][0]['record_id'] = 999
        self.assertEqual(self.client.post('/api/restore', json=broken).status_code, 400)
        broken = copy.deepcopy(backup)
        broken['tables']['payments'].append(broken['tables']['payments'][0])
        self.assertEqual(self.client.post('/api/restore', json=broken).status_code, 400)
        self.assertEqual(self.data()['totals']['total_pay'], 120)
        for key in ['rejections', 'attendance_bonuses', 'payments']:
            del backup['tables'][key]
        self.assertEqual(self.client.post('/api/restore', json=backup).status_code, 200)
        self.assertEqual(self.data()['totals']['total_pay'], 25)
        self.assertEqual(self.data()['payments'], [])

    def test_csv_and_pending_bonus(self):
        record = self.record()
        self.reject(record, 2)
        csv = self.client.get('/api/export?month=2024-02').content.decode('utf-8-sig')
        self.assertIn('计薪数量', csv)
        self.assertIn('尺寸不合格', csv)
        self.assertIn('2024-02', self.data()['reconciliation']['unconfirmed_bonus_months'])
        self.bonus(False)
        self.assertEqual(self.data()['reconciliation']['unconfirmed_bonus_months'], [])
