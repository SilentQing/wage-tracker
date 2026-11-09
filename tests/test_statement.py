import csv
import io

from test_overtime import LedgerTestCase


class StatementTests(LedgerTestCase):
    def statement(self, month='2024-02'):
        response = self.client.get('/api/export', params=dict(month=month, kind='statement'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'\xef\xbb\xbf'))
        self.assertIn(f'wage-statement-{month}.csv', response.headers['content-disposition'])
        return list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))

    def test_monthly_amounts_details_and_scope(self):
        record = self.client.post('/api/records', json=dict(record_date='2024-02-10', part_name='=零件', quantity=3, unit_price='0.335', note='+备注')).json()['id']
        self.client.put(f'/api/records/{record}/rejection', json=dict(quantity=1, reason='@原因'))
        self.client.put('/api/daily-attendance', json=dict(month='2024-02'))
        self.client.put('/api/leave', json=dict(work_date='2024-02-10', hours=4.25, note='-请假'))
        self.mark('2024-02-10')
        self.mark('2024-03-01')
        self.client.put('/api/attendance-bonus', json=dict(month='2024-02', awarded=True))
        for key, value in [('statement-pay-01', '50.00'), ('statement-pay-02', '80.00')]:
            response = self.client.post('/api/payments', json=dict(id=key, month='2024-02', paid_date='2024-03-10', amount=value, note='=到账'))
            self.assertEqual(response.status_code, 200)
        self.client.delete('/api/payments/statement-pay-02')
        self.client.put('/api/month-lock', json=dict(month='2024-02', locked=True))
        rows = self.statement()
        values = {row[0]: row[1] for row in rows if len(row) == 2}
        data = self.data()
        self.assertEqual(values['原计件金额（元）'], '1.01')
        self.assertEqual(values['不合格扣件（元）'], '0.34')
        for label, value in [('应发工资（元）', data['totals']['total_pay']), ('有效到账合计（元）', data['reconciliation']['paid']), ('未到账金额（元）', data['reconciliation']['difference'])]:
            self.assertEqual(values[label], f'{value:.2f}')
        self.assertEqual(values['请假时长（小时）'], '4.25')
        self.assertEqual(values['锁定状态'], '已锁定')
        self.assertTrue(any('已作废（不计入合计）' in row for row in rows))
        self.assertTrue(any('2024-03-10' in row for row in rows))
        self.assertFalse(any('2024-03-01' in row for row in rows))
        for text in ["'=零件", "'+备注", "'@原因", "'-请假", "'=到账"]:
            self.assertTrue(any(text in row for row in rows), text)

    def test_pending_legacy_and_deleted_records(self):
        self.attendance(count=2)
        self.client.post('/api/records', json=dict(record_date='2024-02-10', part_name='待核价', quantity=2))
        removed = self.client.post('/api/records', json=dict(record_date='2024-02-11', part_name='已删除零件', quantity=2, unit_price=99)).json()['id']
        self.client.delete(f'/api/records/{removed}')
        rows = self.statement()
        text = str(rows)
        self.assertIn('待定价', text)
        self.assertIn('全勤奖未确认', text)
        self.assertIn('未记录（旧月度考勤）', text)
        self.assertIn('未分配日期 2 次', text)
        self.assertNotIn('已删除零件', text)
        self.assertIn(['应发工资（元）', '20.00'], rows)

    def test_empty_month_and_invalid_periods(self):
        self.assertIn(['应发工资（元）', '0.00'], self.statement())
        for month in ['ALL', '2024-13', 'bad']:
            self.assertEqual(self.client.get('/api/export', params=dict(month=month, kind='statement')).status_code, 400)
