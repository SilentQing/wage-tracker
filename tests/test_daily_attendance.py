from datetime import date
from unittest.mock import patch
from test_overtime import LedgerTestCase


class DailyAttendanceTests(LedgerTestCase):
    def enable(self, month='2024-02'):
        response = self.client.put('/api/daily-attendance', json={'month': month})
        self.assertEqual(response.status_code, 200)

    def leave(self, day='2024-02-10', hours=4.25):
        return self.client.put('/api/leave', json={'work_date': day, 'hours': hours, 'note': '请假'})

    def test_full_month_weekends_and_leave(self):
        self.enable()
        self.assertEqual(self.data()['totals']['regular_hours'], 29 * 8.5)
        self.assertEqual(self.leave().status_code, 200)
        self.assertEqual(self.data()['totals']['work_days'], 28.5)
        self.assertEqual(self.leave(hours=8.5).status_code, 200)
        self.assertEqual(self.data()['totals']['work_days'], 28)
        self.leave(hours=0)
        self.assertEqual(self.data()['totals']['work_days'], 29)

    def test_current_month_cutoff_and_lock_freeze(self):
        with patch.object(self.module, 'date', wraps=date) as clock:
            clock.max = date.max
            clock.today.return_value = date(2024, 2, 10)
            self.enable()
            self.assertEqual(self.data()['totals']['regular_hours'], 85)
            self.assertEqual(self.leave('2024-02-11').status_code, 400)
            self.assertEqual(self.client.put('/api/month-lock', json={'month':'2024-02','locked':True}).status_code, 200)
            clock.today.return_value = date(2024, 2, 11)
            self.assertEqual(self.data()['totals']['regular_hours'], 85)
            self.assertEqual(self.leave().status_code, 409)
            self.client.put('/api/month-lock', json={'month':'2024-02','locked':False})
            self.assertEqual(self.data()['totals']['regular_hours'], 93.5)

    def test_old_attendance_and_independent_months(self):
        self.attendance()
        self.assertEqual(self.data()['totals']['regular_hours'], 160)
        self.enable('2024-03')
        self.assertEqual(self.data()['totals']['regular_hours'], 160)
        self.assertEqual(self.data('2024-03')['totals']['regular_hours'], 263.5)
        self.assertEqual(self.data('ALL')['totals']['regular_hours'], 423.5)
        self.assertEqual(self.leave().status_code, 409)

    def test_backup_roundtrip_and_old_backup(self):
        self.enable()
        self.leave(hours=2)
        backup = self.client.get('/api/backup').json()
        self.leave(hours=8.5)
        self.assertEqual(self.client.post('/api/restore', json=backup).status_code, 200)
        self.assertEqual(self.data()['attendance']['leave_hours'], 2)
        del backup['tables']['daily_months']
        del backup['tables']['leave_days']
        self.assertEqual(self.client.post('/api/restore', json=backup).status_code, 200)
        self.assertFalse(self.data()['attendance'].get('daily_mode', False))

    def test_validation_and_future_month(self):
        self.enable()
        self.assertEqual(self.leave(hours=8.51).status_code, 422)
        self.assertEqual(self.leave(hours=-1).status_code, 422)
        self.enable('2099-01')
        self.assertEqual(self.data('2099-01')['totals']['regular_hours'], 0)
        self.assertEqual(self.client.put('/api/daily-attendance', json={'month':'bad'}).status_code, 400)
