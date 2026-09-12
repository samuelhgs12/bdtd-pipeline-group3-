import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from bdtd.core import Store, parse_oai
from bdtd.network import Client, harvest

SAMPLE = (Path(__file__).resolve().parents[1] / 'examples/synthetic.xml').read_bytes()

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_idempotent_and_preserves_repeated_fields(self):
        self.store.archive(SAMPLE, 'synthetic://test', True)
        self.store.staging()
        before = (Path(self.temp.name) / 'staging/metadados.jsonl').read_bytes()
        self.store.archive(SAMPLE, 'synthetic://test', True)
        self.store.staging()
        after = (Path(self.temp.name) / 'staging/metadados.jsonl').read_bytes()
        self.assertEqual(before, after)
        rows = [json.loads(line) for line in after.splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(rows[0]['dc']['date']), 2)
        self.assertEqual(rows[0]['dc']['description'][1]['lang'], 'en')
        self.assertTrue(rows[2]['deleted'])
        self.assertTrue(all(row['synthetic'] for row in rows))

    def test_rejects_html_and_protocol_error(self):
        for body in (b'<html>challenge</html>', b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="badResumptionToken">expired</error></OAI-PMH>'):
            with self.assertRaises(ValueError):
                parse_oai(body, 'ListRecords')

    def test_resume_pagination_with_exclusive_token(self):
        first = SAMPLE.replace(b'</ListRecords>', b'<resumptionToken>a+b/=</resumptionToken></ListRecords>')
        urls = []
        class Fake:
            def get(self, url):
                urls.append(url)
                return first if len(urls) == 1 else SAMPLE
        client = Fake()
        self.assertEqual(harvest(self.store, client, 'https://example.org/oai', 'demo', 1), 1)
        self.assertEqual(harvest(self.store, client, 'https://example.org/oai', 'demo', 1), 1)
        self.assertEqual(parse_qs(urlsplit(urls[1]).query), {'verb':['ListRecords'], 'resumptionToken':['a+b/=']})
        self.assertEqual(harvest(self.store, client, 'https://example.org/oai', 'demo', 1), 0)
        self.store.staging()
        self.assertEqual(self.store.export(), 3)

    def test_failed_request_does_not_advance_checkpoint(self):
        class Broken:
            def get(self, url):
                raise TimeoutError('simulated')
        with self.assertRaises(TimeoutError):
            harvest(self.store, Broken(), 'https://example.org/oai', 'demo', 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_checksum_detects_corruption(self):
        sha = self.store.archive(SAMPLE, 'synthetic://test')
        (Path(self.temp.name) / 'raw/oai' / (sha+'.xml')).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.store.staging()

    def test_invalid_record_is_counted(self):
        self.store.archive(SAMPLE.replace(b'<identifier>oai:synthetic:001</identifier>', b''), 'synthetic://test')
        self.store.staging()
        self.assertEqual(self.store.export(), 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM rejects').fetchone()[0], 1)

    def test_older_version_does_not_restore_deleted_record(self):
        self.store.archive(SAMPLE, 'synthetic://test')
        self.store.staging()
        newer = SAMPLE.replace(b'<header><identifier>oai:synthetic:001', b'<header status="deleted"><identifier>oai:synthetic:001').replace(b'2026-01-01', b'2026-02-01')
        self.store.archive(newer, 'synthetic://new')
        self.store.staging()
        older = SAMPLE.replace(b'2026-01-01', b'2025-01-01')
        self.store.archive(older, 'synthetic://old')
        self.store.staging()
        payload = self.store.db.execute('SELECT payload FROM records WHERE id=?', ('oai:synthetic:001',)).fetchone()[0]
        self.assertTrue(json.loads(payload)['deleted'])

    def test_robots_denial_and_challenge(self):
        for body in (b'User-agent: *\nDisallow: /', b'<html>challenge</html>'):
            client = Client('Test/1')
            with patch.object(client, 'request', return_value=body) as mocked:
                with self.assertRaises((ValueError, PermissionError)):
                    client.get('https://example.org/oai')
                self.assertEqual(mocked.call_count, 1)

if __name__ == '__main__':
    unittest.main()
