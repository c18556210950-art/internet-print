import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipIf(sys.platform == 'win32', 'The server requires Linux (fcntl).')
class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        # Existing installations must keep their jobs when print options are added.
        db = sqlite3.connect(data / 'print.db')
        db.execute('''CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, claimed_at TEXT, finished_at TEXT)''')
        db.execute("INSERT INTO jobs (original_name,stored_name,content_type,size,status,created_at) VALUES ('existing.pdf','old.pdf','application/pdf',1,'done','2026-01-01')")
        db.commit()
        db.close()
        cls.old_env = {k: os.environ.get(k) for k in ['PRINT_DATA_DIR','PRINT_PASSWORD','PRINT_AGENT_TOKEN','PRINT_SECRET']}
        os.environ.update(PRINT_DATA_DIR=str(data), PRINT_PASSWORD='test-only-password',
                          PRINT_AGENT_TOKEN='test-only-token', PRINT_SECRET='test-only-secret')
        server = Path(__file__).resolve().parents[1] / 'server'
        sys.path.insert(0, str(server))
        spec = importlib.util.spec_from_file_location('portal_under_test', server / 'app.py')
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True, SESSION_COOKIE_PATH='/', SESSION_COOKIE_SECURE=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        for key, value in cls.old_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['logged_in'] = True
        self.auth = {'Authorization': 'Bearer test-only-token'}
        with self.app.app_context():
            db = self.module.get_db()
            db.execute("DELETE FROM jobs WHERE status != 'done'")
            db.commit()

    def test_legacy_database_migration(self):
        with self.app.app_context():
            row = self.module.get_db().execute("SELECT * FROM jobs WHERE original_name='existing.pdf'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual((row['sides'], row['orientation']), ('simplex','portrait'))

    def test_agent_authentication(self):
        self.assertEqual(self.client.get('/api/agent/next').status_code, 401)
        self.assertEqual(self.client.get('/api/agent/next', headers={'Authorization':'Bearer wrong'}).status_code, 401)

    def test_upload_requires_login(self):
        anonymous = self.app.test_client()
        response = anonymous.post('/upload', data={'file': (io.BytesIO(b'%PDF-test'), 'test.pdf')})
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.location)

    def test_task_settings_survive_queue(self):
        for sides in ('simplex','duplexlong','duplexshort'):
            for orientation in ('portrait','landscape'):
                with self.subTest(sides=sides, orientation=orientation):
                    response = self.client.post('/upload', data={
                        'file': (io.BytesIO(b'%PDF-test'), 'test.pdf'),
                        'sides': sides, 'orientation': orientation})
                    self.assertEqual(response.status_code, 302)
                    job = self.client.get('/api/agent/next', headers=self.auth).get_json()
                    self.assertEqual((job['sides'],job['orientation']), (sides,orientation))
                    file_response = self.client.get(f"/api/agent/jobs/{job['id']}/file",headers=self.auth)
                    self.assertEqual(file_response.data,b'%PDF-test')
                    file_response.close()
                    self.assertEqual(self.client.post(f"/api/agent/jobs/{job['id']}/status",headers=self.auth,
                        json={'status':'done'}).status_code,200)

    def test_invalid_options_do_not_queue(self):
        for settings in [{'sides':'invalid'},{'orientation':'invalid'}]:
            response = self.client.post('/upload',data={**settings,'file':(io.BytesIO(b'%PDF-test'),'test.pdf')})
            self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get('/api/agent/next',headers=self.auth).status_code,204)

    def test_form_and_job_api(self):
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'name="sides"',response.data)
        self.assertIn(b'name="orientation"',response.data)
        self.assertEqual(self.client.get('/api/jobs').status_code,200)


if __name__ == '__main__':
    unittest.main()
