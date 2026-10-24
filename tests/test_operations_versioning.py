from datetime import date
from http.server import ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread
import unittest
from unittest.mock import patch

from app.config import Settings
from app.operations import OperationLog
from app.periods import timestamp
from app.server import Application, handler_for
from app.versioning import backend_build, page_build
from tests.helpers import temporary_directory


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.app = Application(self.settings)
        self.app.storage.save_messages('demo', [{
            'message_id': 'message', 'chat_id': 'a', 'sender_id': 'me', 'sender_name': '张工',
            'created_at': timestamp(date(2026, 10, 20)), 'text': '已完成接口开发',
        }], chats=[{'chat_id': 'a', 'name': '虚构群'}])

    def generate(self):
        return self.app.post('/api/reports', {'source': 'demo', 'chat_ids': ['a'],
            'period': 'daily', 'anchor_date': '2026-10-20', 'audience': 'personal',
            'sender_ids': ['me']})['report']

    def test_receipts_are_allowlisted_and_do_not_copy_free_text(self):
        report = self.generate()
        self.app.post('/api/identity', {'source': 'demo', 'sender_id': 'me', 'mode': 'nickname',
            'nickname': 'private-nickname', 'secret': 'private-secret'})
        self.app.post('/api/reports/' + report['id'] + '/revisions',
            {'markdown': 'private-body', 'notes': 'private-notes'})
        rows = self.app.operations.list()['operations']
        raw = json.dumps(rows, ensure_ascii=False)
        for forbidden in ('private-nickname', 'private-secret', 'private-body', 'private-notes', '已完成接口开发'):
            self.assertNotIn(forbidden, raw)
        self.assertEqual(rows[1]['display_mode'], 'nickname')
        self.assertEqual(rows[0]['related_id'], report['id'])
        self.assertEqual(rows[0]['counts']['message_count'], 1)
        self.assertEqual(rows[0]['chat_ids'], ['a'])

    def test_failed_validation_has_safe_receipt_and_no_free_form_chat(self):
        with self.assertRaises(ValueError):
            self.app.post('/api/reports', {'source': 'demo', 'chat_ids': ['private-secret'], 'period': 'bad'})
        row = self.app.operations.list()['operations'][0]
        self.assertEqual((row['status'], row['error_code'], row['chat_ids']), ('failed', 'invalid_input', []))
        self.assertNotIn('private-secret', json.dumps(row))
        with self.assertRaises(ValueError):
            self.app.post('/api/identity', {'source': {'secret': 'private-secret'}})
        self.assertNotIn('private-secret', json.dumps(self.app.operations.list()))

    def test_restart_retains_receipts_and_recovers_unfinished_without_claiming_success(self):
        self.generate()
        unfinished = self.app.operations.begin('sync_messages', {'source': 'demo', 'chat_ids': ['a']})
        restarted = Application(self.settings)
        restarted.operations.recover_interrupted()
        rows = restarted.operations.list()['operations']
        self.assertEqual(rows[0]['id'], unfinished)
        self.assertEqual(rows[0]['status'], 'interrupted')
        self.assertEqual(rows[1]['status'], 'succeeded')
        self.assertNotEqual(restarted.instance_id, self.app.instance_id)

    def test_restore_does_not_roll_back_operation_ledger(self):
        first = self.generate()
        backup = self.app.post('/api/backups', {})['backup']
        second = self.generate()
        before = self.app.operations.list()['total']
        self.app.post('/api/backups/restore', {'backup_id': backup['id']})
        self.assertIsNone(self.app.storage.report(second['id']))
        self.assertIsNotNone(self.app.storage.report(first['id']))
        self.assertEqual(self.app.operations.list()['total'], before + 1)
        rows = Application(self.settings).operations.list()['operations']
        self.assertEqual(rows[0]['action'], 'restore_backup')
        self.assertEqual(rows[0]['status'], 'succeeded')
        self.assertTrue(any(r.get('report_id') == second['id'] for r in rows))

    def test_begin_failure_prevents_mutation_and_finish_failure_preserves_success(self):
        with patch.object(self.app.operations, 'begin', side_effect=sqlite3.OperationalError('private-disk-error')):
            with self.assertRaisesRegex(ValueError, '未开始'):
                self.generate()
        self.assertEqual(len(self.app.storage.reports()), 0)
        with patch.object(self.app.operations, 'finish', side_effect=sqlite3.OperationalError('private-disk-error')):
            result = self.app.post('/api/identity', {'source': 'demo', 'sender_id': 'me', 'mode': 'nickname', 'nickname': '本机名称'})
        self.assertIn('operation_warning', result)
        self.assertEqual(self.app.storage.identity('demo', 'me')['display_name'], '本机名称')
        self.assertEqual(self.app.operations.list()['total'], 1)
        self.assertNotIn('private-disk-error', json.dumps(result))

    def test_review_route_records_counts_without_item_text(self):
        original = self.generate()
        item = original['items'][0]
        reviewed = self.app.post('/api/reports/' + original['id'] + '/review', {
            'changes': [{'item_id': item['item_id'], 'included': True, 'category': 'plan', 'text': 'private-rewrite'}],
            'additions': [{'text': 'private-addition', 'category': 'completed'}], 'notes': 'private-notes',
        })['report']
        row = self.app.operations.list()['operations'][0]
        self.assertEqual(row['action'], 'review_report')
        self.assertEqual(row['report_id'], reviewed['id'])
        self.assertEqual(row['counts']['review_item_count'], 2)
        self.assertEqual(row['counts']['manual_item_count'], 2)
        self.assertEqual(self.app.storage.report(original['id'])['markdown'], original['markdown'])
        self.assertNotIn('private-', json.dumps(row))

    def test_metadata_failure_after_success_does_not_report_business_failure(self):
        original = self.app._operation_metadata
        def metadata(path, body, result=None):
            if result is not None:
                raise sqlite3.OperationalError('private-disk-error')
            return original(path, body, result)
        with patch.object(self.app, '_operation_metadata', side_effect=metadata):
            result = self.app.post('/api/identity', {'source': 'demo', 'sender_id': 'me',
                'mode': 'nickname', 'nickname': '本机名称'})
        self.assertIn('operation_warning', result)
        self.assertEqual(self.app.storage.identity('demo', 'me')['display_name'], '本机名称')
        self.assertNotIn('private-disk-error', json.dumps(result))

    def test_constructing_second_application_does_not_interrupt_live_receipts(self):
        pending = self.app.operations.begin('sync_messages', {'source': 'demo'})
        second = Application(self.settings)
        self.assertEqual(second.operations.list()['operations'][0]['id'], pending)
        self.assertEqual(second.operations.list()['operations'][0]['status'], 'running')

    def test_duplicate_server_start_does_not_recover_receipts(self):
        from app import server as server_module
        with patch.object(server_module, 'load_settings', return_value=self.settings), \
             patch.object(server_module, 'ThreadingHTTPServer', side_effect=OSError('port busy')), \
             patch.object(OperationLog, 'recover_interrupted') as recovery:
            with self.assertRaises(SystemExit):
                server_module.main()
            recovery.assert_not_called()

    def test_background_job_is_logged_once_with_its_scope(self):
        entered, release = Event(), Event()
        real_sync = self.app.sync
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return real_sync(*args, **kwargs)
        with patch.object(self.app, 'sync', side_effect=blocked):
            job = self.app.post('/api/jobs', {'action': 'sync', 'source': 'demo', 'chat_ids': ['a'],
                'start_date': '2026-10-20', 'end_date': '2026-10-20'})['job']
            self.assertTrue(entered.wait(5))
            running = self.app.operations.list()['operations'][0]
            self.assertEqual(running['status'], 'running')
            self.assertEqual(running['job_id'], job['id'])
            release.set()
            # Job completion can be observed without arbitrary sleeps.
            for _ in range(100):
                if self.app.jobs.get(job['id'])['status'] in {'succeeded', 'failed'}:
                    break
                Event().wait(.02)
        row = self.app.operations.list()['operations'][0]
        self.assertEqual(row['status'], 'succeeded')
        self.assertEqual(row['counts']['synced_count'], 1)
        self.assertEqual(self.app.operations.list()['total'], 1)

    def test_pagination_bounds_and_revision_excludes_backups(self):
        initial = self.app.operations.revision()
        self.app.post('/api/backups', {})
        self.assertEqual(self.app.operations.revision(), initial)
        self.generate()
        self.assertNotEqual(self.app.operations.revision(), initial)
        for limit in ('0', '101', 'abc'):
            with self.assertRaises(ValueError):
                self.app.get('/api/operations', {'limit': [limit]})

    def test_version_metadata_detects_backend_change_but_does_not_read_data(self):
        info = self.app.get('/api/version', {})
        self.assertEqual(info['running_build'], info['backend_build'])
        self.assertEqual(info['environment'], 'demo')
        with patch('app.server.backend_build', return_value='changed'):
            self.assertNotEqual(self.app.get('/api/version', {})['backend_build'], info['running_build'])
        self.assertNotIn('secret', json.dumps(info).lower())


class BuildTests(unittest.TestCase):
    def test_code_and_static_change_fingerprints_but_data_and_env_do_not(self):
        with temporary_directory() as temp:
            root = Path(temp)
            (root / 'static').mkdir()
            (root / 'app.py').write_text('first')
            (root / 'static' / 'app.js').write_text('first')
            backend, page = backend_build(root), page_build(root)
            (root / '.env').write_text('private-secret')
            (root / 'data.sqlite3').write_text('private-body')
            self.assertEqual((backend, page), (backend_build(root), page_build(root)))
            (root / 'static' / 'app.js').write_text('second')
            self.assertEqual(backend, backend_build(root))
            self.assertNotEqual(page, page_build(root))
            (root / 'app.py').write_text('second')
            self.assertNotEqual(backend, backend_build(root))


class VersionHTTPTests(unittest.TestCase):
    def test_html_meta_is_injected_and_new_script_is_served(self):
        with temporary_directory() as temp:
            app = Application(Settings(data_dir=Path(temp)))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(app))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for path in ('/', '/workspace.js'):
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    conn.request('GET', path)
                    response = conn.getresponse()
                    data = response.read().decode('utf-8')
                    self.assertEqual(response.status, 200)
                    self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
                    if path == '/':
                        self.assertIn(app.instance_id, data)
                        self.assertIn(page_build(), data)
                        self.assertNotIn('__WORK_SUMMARY_', data)
                    conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
