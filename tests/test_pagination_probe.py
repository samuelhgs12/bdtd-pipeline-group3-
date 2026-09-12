import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bdtd.pagination_probe import PaginationProbe


CONFIG = {'endpoint': 'https://example.org/api', 'lookfor': 'x', 'type': 'Subject',
          'filters': [], 'limit': 20}


class Fake:
    def get(self, url):
        params = parse_qs(urlsplit(url).query)
        page = int(params['page'][0])
        limit = int(params['limit'][0])
        records = [dict(id=f'{limit}-{page}-{i}', title='T') for i in range(limit)]
        return json.dumps(dict(status='OK', resultCount=1000, records=records)).encode()


class PaginationProbeTests(unittest.TestCase):
    def test_archives_and_resumes_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = PaginationProbe(directory, CONFIG)
            report = probe.run(Fake(), cases=((1, 1), (20, 2)))
            probe.close_calls = None
            probe.db.close()
            self.assertEqual(report['probes'], 2)
            self.assertEqual(report['api_totals'], [1000])
            self.assertTrue((Path(directory) / 'reports/pagination_probe.json').exists())
            resumed = PaginationProbe(directory, CONFIG)
            self.assertEqual(resumed.run(Fake(), cases=((1, 1),))['probes'], 2)
            resumed.db.close()


if __name__ == '__main__':
    unittest.main()
