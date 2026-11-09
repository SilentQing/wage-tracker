import copy
from concurrent.futures import ThreadPoolExecutor

from test_overtime import LedgerTestCase


class SubmissionTests(LedgerTestCase):
    def record_payload(self, key='record-request-0001'):
        return dict(request_id=key, submission_epoch=self.data()['submission_epoch'],
                    record_date='2024-02-10', part_name='轴套', quantity=3, unit_price='0.335', note='原提交')

    def check(self, kind, key, epoch, cancel=False):
        url = f'/api/submissions/{kind}/{key}' + ('/cancel' if cancel else '')
        return (self.client.post if cancel else self.client.get)(url, params={'epoch': epoch})

    def test_record_concurrent_dedup_conflict_and_legitimate_identical_entries(self):
        payload = self.record_payload()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.client.post('/api/records', json=payload), range(2)))
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(responses[0].json()['id'], responses[1].json()['id'])
        self.assertEqual(len(self.data()['records']), 1)
        self.assertEqual(self.client.post('/api/records', json={**payload, 'quantity': 9}).status_code, 409)
        self.assertEqual(self.client.post('/api/records', json={**payload, 'request_id': 'record-request-0002'}).status_code, 200)
        self.assertEqual(len(self.data()['records']), 2)

    def test_original_fingerprint_survives_price_edit_lock_and_deletion(self):
        payload = self.record_payload()
        payload.pop('unit_price')
        self.client.post('/api/price', json=dict(part_name='轴套', unit_price=1))
        record = self.client.post('/api/records', json=payload).json()['id']
        self.client.post('/api/price', json=dict(part_name='轴套', unit_price=9))
        self.client.put(f'/api/records/{record}', json=dict(record_date='2024-02-10', part_name='轴套', quantity=8, unit_price=2))
        self.client.delete(f'/api/records/{record}')
        self.client.put('/api/month-lock', json=dict(month='2024-02', locked=True))
        replay = self.client.post('/api/records', json=payload)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['deleted'], 1)
        self.assertEqual(self.data()['records'], [])
        self.assertEqual(self.data()['deleted_records'][0]['quantity'], 8)
        self.assertEqual(self.check('records', payload['request_id'], payload['submission_epoch']).json()['deleted'], 1)

    def test_payment_retry_reports_voided_and_does_not_reactivate(self):
        epoch = self.data()['submission_epoch']
        payload = dict(id='payment-request-01', submission_epoch=epoch, month='2024-02', paid_date='2024-03-10', amount='10.00', note='工资')
        self.client.post('/api/payments', json=payload)
        self.client.delete('/api/payments/' + payload['id'])
        self.assertEqual(self.client.post('/api/payments', json=payload).json()['voided'], 1)
        self.assertEqual(self.check('payments', payload['id'], epoch, cancel=True).json()['state'], 'saved')
        self.assertEqual(self.data()['reconciliation']['paid'], 0)

    def test_cancel_blocks_late_record_and_payment_requests(self):
        record = self.record_payload()
        epoch = record['submission_epoch']
        payment = dict(id='payment-request-01', submission_epoch=epoch, month='2024-02', paid_date='2024-03-10', amount='10.00')
        for kind, key, payload in [('records', record['request_id'], record), ('payments', payment['id'], payment)]:
            self.assertEqual(self.check(kind, key, epoch).json()['state'], 'absent')
            self.assertEqual(self.check(kind, key, epoch, cancel=True).json()['state'], 'cancelled')
            self.assertEqual(self.check(kind, key, epoch, cancel=True).json()['state'], 'cancelled')
            self.assertEqual(self.client.post('/api/' + kind, json=payload).status_code, 409)
        self.assertEqual(self.data()['records'], [])

    def test_backup_restore_preserves_receipts_changes_epoch_and_accepts_legacy(self):
        payload = self.record_payload()
        self.client.post('/api/records', json=payload)
        self.check('payments', 'cancel-payment-0001', payload['submission_epoch'], cancel=True)
        backup = self.client.get('/api/backup').json()
        self.assertEqual(len(backup['tables']['record_requests']), 1)
        self.assertEqual(self.client.post('/api/restore/preview', json=backup).status_code, 200)
        self.assertEqual(self.client.post('/api/restore', json=backup).status_code, 200)
        self.assertNotEqual(self.data()['submission_epoch'], payload['submission_epoch'])
        self.assertEqual(self.client.post('/api/records', json=payload).status_code, 409)
        self.assertEqual(self.check('records', payload['request_id'], payload['submission_epoch']).status_code, 409)
        payload['submission_epoch'] = self.data()['submission_epoch']
        self.assertEqual(self.client.post('/api/records', json=payload).status_code, 200)
        self.assertEqual(len(self.data()['records']), 1)
        broken = copy.deepcopy(backup)
        broken['tables']['record_requests'][0]['record_id'] = 99999
        self.assertEqual(self.client.post('/api/restore', json=broken).status_code, 400)
        broken = copy.deepcopy(backup)
        broken['tables']['submission_cancellations'].append(dict(kind='records', request_id=payload['request_id']))
        self.assertEqual(self.client.post('/api/restore', json=broken).status_code, 400)
        for key in ['record_requests', 'submission_cancellations']:
            backup['tables'].pop(key)
        self.assertEqual(self.client.post('/api/restore', json=backup).status_code, 200)
        self.assertEqual(len(self.data()['records']), 1)
