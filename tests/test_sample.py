import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

from bdtd.sample import Sampler

CONFIG={'endpoint':'https://example.org/api','join':'AND','groups':[{'operator':'OR','clauses':[
    {'lookfor':'Linguagem','type':'Subject'},{'lookfor':'Comunicação','type':'Abstract'}]}],
    'filters':['~format:"masterThesis"'],'limit':20}


class Fake:
    def __init__(self,total): self.total=total; self.calls=[]
    def get(self,url):
        self.calls.append(url)
        params=parse_qs(urlsplit(url).query)
        page=int(params['page'][0])
        rows=[] if page>self.total else [dict(id=f'id-{page}',title=f'Título {page}',
            formats=['masterThesis'],subjects=[['Linguagem']],urls=[])]
        return json.dumps(dict(status='OK',resultCount=self.total,records=rows)).encode()


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.sampler=Sampler(self.tmp.name,CONFIG,seed=42,size=5,frame_size=100)
    def tearDown(self):
        self.sampler.db.close(); self.tmp.cleanup()

    def test_reproducible_distributed_sample_and_boundary(self):
        report=self.sampler.run(Fake(100))
        rows=[json.loads(x) for x in (Path(self.tmp.name)/'selected_50.jsonl').read_text().splitlines()]
        self.assertEqual([r['sample_rank'] for r in rows],[82,15,4,95,36])
        self.assertEqual(report['unique_record_ids'],5)
        self.assertTrue(report['accessible_frame_boundary_reachable'])
        self.assertEqual(report['position_after_frame'],'vazia_no_fim_da_populacao')
        self.assertGreater(sum(bool(x) for x in report['position_deciles_within_accessible_frame']),1)

    def test_resume_does_not_repeat_pages(self):
        client=Fake(100)
        self.sampler.run(client)
        calls=len(client.calls)
        self.sampler.run(client)
        self.assertEqual(len(client.calls),calls)

    def test_changed_seed_cannot_mix(self):
        with self.assertRaises(ValueError): Sampler(self.tmp.name,CONFIG,seed=43,size=5,frame_size=100)

    def test_clamped_window_is_reported_and_not_called_population_sample(self):
        class Clamped(Fake):
            def get(self,url):
                params=parse_qs(urlsplit(url).query)
                page=min(int(params['page'][0]),100)
                rows=[dict(id=f'id-{page}',title=f'Título {page}',formats=['masterThesis'],
                           subjects=[],urls=[])]
                return json.dumps(dict(status='OK',resultCount=1000,records=rows)).encode()
        report=self.sampler.run(Clamped(1000))
        self.assertFalse(report['population_random_sample'])
        self.assertTrue(report['position_after_frame_repeats_boundary'])
        self.assertEqual(report['accessible_frame_size'],100)

    def test_duplicate_ranked_record_stops_selection(self):
        class Duplicate(Fake):
            def get(self,url):
                data=json.loads(super().get(url))
                if data['records']: data['records'][0]['id']='same'
                return json.dumps(data).encode()
        with self.assertRaisesRegex(ValueError,'ID repetido'):
            self.sampler.run(Duplicate(100))


if __name__=='__main__': unittest.main()
