import json
import tempfile
import unittest
from pathlib import Path
from bdtd.api import Pilot, normalize, validate, build_url
from urllib.parse import parse_qs, urlsplit

CONFIG = {'endpoint':'https://example.org/api', 'lookfor':'Linguagens OR Comunicação',
          'type':'Subject', 'filters':['~format:"masterThesis"','~format:"doctoralThesis"'],
          'limit':2, 'browser_reference_total':4}

def response(ids, total=4):
    return json.dumps(dict(status='OK',resultCount=total, records=[dict(id=str(i),title='Título '+str(i),
               formats=['masterThesis'],subjects=[['Comunicação'], 'Linguagens'],urls=[]) for i in ids])).encode()

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pilot = Pilot(self.tmp.name, CONFIG)
    def tearDown(self):
        self.pilot.db.close()
        self.tmp.cleanup()
    def test_resume_and_no_duplicate_requests(self):
        class Fake:
            def __init__(self): self.calls=0
            def get(self, url):
                self.calls += 1
                p=int(parse_qs(urlsplit(url).query)['page'][0])
                return response([1,2] if p==1 else [3,4])
        client=Fake()
        self.pilot.run(client,1)
        self.pilot.run(client,2)
        self.pilot.run(client,2)
        self.assertEqual(client.calls,2)
        report=self.pilot.export()
        self.assertEqual(report['records_collected'],4)
        self.assertTrue(report['pagination_observed'])
        self.assertFalse(report['area_validated'])
    def test_repeated_page_is_stopped(self):
        class Fake:
            def get(self,url): return response([1,2])
        with self.assertRaisesRegex(ValueError,'repetido'):
            self.pilot.run(Fake(),2)
        report=json.loads((Path(self.tmp.name)/'reports/api_pilot.json').read_text())
        self.assertEqual(report['records_collected'],2)
        self.assertIsNotNone(report['error'])
    def test_invalid_status_is_stopped(self):
        with self.assertRaises(ValueError): validate(b'{"status":"ERROR"}')
        with self.assertRaises(ValueError): validate(response([1,1]))
    def test_filters_and_subject_query_preserved(self):
        params=parse_qs(urlsplit(build_url(CONFIG,2)).query)
        self.assertEqual(params['filter[]'],CONFIG['filters'])
        self.assertEqual(params['type'],['Subject'])
        self.assertEqual(params['lookfor'],['Linguagens OR Comunicação'])
    def test_normalization_keeps_original(self):
        row=validate(response([1,2]))['records'][0]
        out=normalize(row,{})
        self.assertEqual(out['subjects'],['Comunicação','Linguagens'])
        self.assertEqual(out['original_record'],row)
        self.assertEqual(out['urls'],[])
    def test_changed_query_cannot_mix(self):
        with self.assertRaises(ValueError): Pilot(self.tmp.name,dict(CONFIG,lookfor='Outra'))
    def test_short_page_not_silently_completed(self):
        class Fake:
            def get(self,url): return response([1])
        with self.assertRaisesRegex(ValueError,'curta'):
            self.pilot.run(Fake(),2)
        self.assertEqual(self.pilot.export()['records_collected'],0)
