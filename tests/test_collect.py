import json
import io
import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.parse import parse_qs,urlsplit,urlencode
from unittest.mock import patch

from bdtd.collect import Collector,TerminalProgress,quoted_filter,year_criterion
from bdtd.api import build_url
from bdtd.core import digest
from bdtd.pdfs import Downloader


CONFIG={'endpoint':'https://example.org/api','lookfor':'x','type':'Subject',
        'filters':['~format:"masterThesis"'],'limit':20}
PDF=b'%PDF-1.4\nqueue fixture\n%%EOF\n'


class Fake:
    def get(self,url):
        query=parse_qs(urlsplit(url).query); filters=query.get('filter[]',[])
        page=int(query['page'][0]); limit=int(query['limit'][0])
        total=120 if 'publishDate:"2024"' in filters else 120
        start=(page-1)*limit
        records=[dict(id=f'id-{i}',title=f'T {i}',formats=['masterThesis'],subjects=[],urls=[])
                 for i in range(start,min(total,start+limit))]
        facets={}
        if 'facet[]' in query:
            if 'publishDate' in query['facet[]']:
                facets['publishDate']=[dict(value='2024',count=120)]
            if 'institution' in query['facet[]']:
                facets['institution']=[dict(value='X',count=120)]
        return json.dumps(dict(status='OK',resultCount=total,records=records,facets=facets)).encode()


class RateLimited:
    def __init__(self): self.calls=[]
    def get(self,url):
        self.calls.append(url)
        error=urllib.error.HTTPError(url,429,'Too Many Requests',{},io.BytesIO())
        error.close()
        raise error


class ValidationFake:
    def get(self,url):
        query=parse_qs(urlsplit(url).query); filters=query.get('filter[]',[])
        filters=[value for value in filters if not value.startswith('~format:')]
        year=(query.get('daterange[]')==['publishDate'] and
              query.get('publishDatefrom')==['1997'] and query.get('publishDateto')==['1997'])
        page=int(query['page'][0]); limit=int(query['limit'][0])
        facets={}
        if not filters and not year:
            total=120
            facets={'publishDate':[dict(value='1997',count=20)],
                    'institution':[dict(value='X',count=30)]}
        elif year and not filters:
            total=20
        elif year and filters==['institution:"X"']:
            total=5
        elif year and filters==['-institution:"X"']:
            total=15
        elif filters==['-publishDate:[* TO *]']:
            total=5
        elif filters==['publishDate:[* TO *]','-publishDate:"1997"']:
            total=95
        else:
            raise AssertionError(filters)
        start=(page-1)*limit
        records=[dict(id=f'{"|".join(filters)}-{i}',title='T',formats=['masterThesis'],subjects=[],urls=[])
                 for i in range(start,min(total,start+limit))]
        return json.dumps(dict(status='OK',resultCount=total,records=records,facets=facets)).encode()


class QueuePDFClient:
    def __init__(self,directory):
        self.directory=Path(directory); self.transferred=0; self.budget=1024**2
        self.record_id=None; self.run_id='test-run'; self.calls=[]
    def fetch(self,url):
        self.calls.append(url); self.transferred+=len(PDF)
        path=self.directory/'queue-download.tmp'; path.write_bytes(PDF)
        return dict(path=str(path),sha256=hashlib.sha256(PDF).hexdigest(),bytes=len(PDF),
                    content_type='application/pdf',charset='utf-8',url=url)


class CollectTests(unittest.TestCase):
    def test_verified_external_pdf_is_counted_without_redownload(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); collector=Collector(root,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            payload=dict(record_id='archived',title='T',urls=['https://repo.example/item'],
                         query_id=collector.signature)
            with collector.db:
                collector.db.execute('INSERT INTO records VALUES(?,?,?)',
                                     ('archived',json.dumps(payload),'now'))
            metadata=collector.export_metadata()[0].read_bytes()
            worker=Downloader(root/'pdfs',metadata,allow_growing_input=True)
            archived_sha='a'*64
            result=dict(record_id='archived',title='T',status='sucesso',
                        files=[dict(path='raw/pdf/aa/missing.pdf',sha256=archived_sha,bytes=10,
                                    url='https://repo.example/file.pdf')],attempts=[],metadata_from_html=[],
                        input_sha256=worker.input_sha,query_id=collector.signature,pipeline_version='test',
                        finished_at=datetime.now(timezone.utc).isoformat(),limited=False,
                        all_attachments_confirmed=False)
            with worker.db:
                worker.db.execute('INSERT INTO results VALUES(?,?)',('archived',json.dumps(result)))
            worker.db.close()
            batches=root/'batches'; batches.mkdir(); db=sqlite3.connect(batches/'batches.sqlite')
            db.executescript('''CREATE TABLE batches(number INTEGER PRIMARY KEY,archive_status TEXT);
              CREATE TABLE files(sha TEXT PRIMARY KEY,batch_number INTEGER,path TEXT,bytes INTEGER);''')
            db.execute('INSERT INTO batches VALUES(?,?)',(1,'transferred_verified'))
            db.execute('INSERT INTO files VALUES(?,?,?,?)',(archived_sha,1,'pdfs/x.pdf',10))
            db.commit(); db.close()
            class NoDownload:
                transferred=0; budget=1000
                def fetch(self,url): raise AssertionError('PDF arquivado não deve ser baixado novamente')
            collector.download_attempted_ids=set(); collector.repository_retry_seconds=0
            attempted,report=collector.download_available('test@example.org',NoDownload())
            self.assertEqual(attempted,0)
            self.assertEqual(report['candidate_work_pdfs'],1)
            collector.db.close()

    def test_mixed_metadata_page_is_committed_individually_and_resume_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            part=collector.add_partition('mixed',[year_criterion(2024)],None,0)
            body=json.dumps(dict(status='OK',resultCount=3,records=[
                dict(id='valid',title='Título',formats=['masterThesis'],subjects=[],urls=[]),
                dict(id='no-title-url',title=None,formats=['masterThesis'],subjects=[],
                     urls=[{'url':'https://repository.example/item'}]),
                dict(id='reject',title=None,formats=['masterThesis'],subjects=[],urls=[])]),ensure_ascii=False).encode()
            request_config=dict(CONFIG,filters=CONFIG['filters'],facets=[],date_ranges=['publishDate'],
                                publishDatefrom='2024',publishDateto='2024',limit=20)
            url=build_url(request_config,1,limit=20)
            raw=Path(directory)/'raw/api'/f'{digest(body)}.json'; raw.parent.mkdir(parents=True)
            raw.write_bytes(body)
            observed=datetime.now(timezone.utc); os.utime(raw,(observed.timestamp(),observed.timestamp()))
            log=Path(directory)/'logs/api_http.jsonl'; log.parent.mkdir(parents=True)
            log.write_text(json.dumps(dict(ts=observed.isoformat(),url=url,status=200,
                                           source='network',bytes=len(body)))+'\n',encoding='utf-8')
            class NoNetwork:
                def get(self,url): raise AssertionError('não deve acessar a rede')
            obj,sha,url=collector.request(NoNetwork(),[year_criterion(2024)],limit=20)
            self.assertEqual(collector.store_records(part,1,obj,sha,url),2)
            self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM records').fetchone()[0],2)
            self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM metadata_rejections').fetchone()[0],1)
            saved=json.loads(collector.db.execute('SELECT payload FROM records WHERE record_id=?',
                                                  ('no-title-url',)).fetchone()[0])
            self.assertIsNone(saved['title'])
            self.assertEqual(saved['metadata_quality_flags'],['titulo_ausente'])
            # Simula interrupção depois da transação da página e antes de avançar next_page.
            self.assertEqual(collector.store_records(part,1,obj,sha,url),2)
            self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM records').fetchone()[0],2)
            self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM metadata_rejections').fetchone()[0],1)
            collector.export_metadata()
            rejects=(Path(directory)/'metadata/rejections.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(rejects),1)
            collector.db.close()

    def test_year_request_uses_date_range_and_preserves_query(self):
        config={'endpoint':'https://example.org/api','join':'AND','groups':[{'operator':'OR','clauses':[
            {'lookfor':'Linguagem','type':'Subject'},{'lookfor':'Linguagem','type':'Abstract'},
            {'lookfor':'Comunicação','type':'Subject'},{'lookfor':'Comunicação','type':'Abstract'}]}],
            'filters':['~format:"masterThesis"','~format:"doctoralThesis"'],'limit':20}
        class Capture:
            def get(self,url):
                self.url=url
                return json.dumps({'status':'OK','resultCount':0,'records':[]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,config,target=5,reserve_bytes=0,batch_bytes=10)
            client=Capture()
            collector.request(client,[year_criterion(2024),quoted_filter('institution','Universidade Exata')],limit=20)
            params=parse_qs(urlsplit(client.url).query)
            self.assertEqual(params['daterange[]'],['publishDate'])
            self.assertEqual(params['publishDatefrom'],['2024'])
            self.assertEqual(params['publishDateto'],['2024'])
            self.assertNotIn('publishDate:"2024"',params['filter[]'])
            self.assertEqual(params['filter[]'],config['filters']+['institution:"Universidade Exata"'])
            self.assertEqual(params['type0[]'],['Subject','Abstract','Subject','Abstract'])
            self.assertEqual(collector.last_api_source,'network')
            collector.db.close()

    def test_selects_exact_archived_institution_by_deterministic_global_count(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            with collector.db:
                collector.db.execute('INSERT INTO facet_values VALUES(?,?,?,?)',
                                     ('institution','Instituição Maior',8000,'now'))
                collector.db.execute('INSERT INTO facet_values VALUES(?,?,?,?)',
                                     ('institution','Nome literal / Campus',1200,'now'))
            self.assertEqual(collector.select_archived_institution(),
                             {'value':'Nome literal / Campus','global_facet_count':1200})
            record={'id':'x','institutions':['Nome literal / Campus'],'title':'T'}
            self.assertEqual(collector.institution_fields(record),
                             {'institutions':['Nome literal / Campus']})
            collector.db.close()

    def test_429_is_not_an_api_cache_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            client=RateLimited()
            for _ in range(2):
                with self.assertRaises(urllib.error.HTTPError): collector.request(client,[])
            self.assertEqual(len(client.calls),2)
            self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM api_cache').fetchone()[0],0)
            collector.db.close()

    def test_imports_compatible_sources_deduplicated_with_provenance_and_reuses_pdf(self):
        config={'endpoint':'https://example.org/api','join':'AND','groups':[{'operator':'OR','clauses':[
            {'lookfor':'Linguagem','type':'Subject'},{'lookfor':'Linguagem','type':'Abstract'},
            {'lookfor':'Comunicação','type':'Subject'},{'lookfor':'Comunicação','type':'Abstract'}]}],
            'filters':['~format:"masterThesis"','~format:"doctoralThesis"'],'limit':20}
        params=[('join','AND'),('bool0[]','OR'),('lookfor0[]','Linguagem'),('type0[]','Subject'),
                ('lookfor0[]','Linguagem'),('type0[]','Abstract'),('lookfor0[]','Comunicação'),
                ('type0[]','Subject'),('lookfor0[]','Comunicação'),('type0[]','Abstract'),
                ('filter[]','~format:"masterThesis"'),('filter[]','~format:"doctoralThesis"')]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); collector=Collector(root/'target',config,5,0,10)
            row=dict(record_id='one',title='T',formats=['masterThesis'],urls=['https://repo/item'],
                     source_url='https://example.org/api?'+urlencode(params),query_id='q')
            a=root/'a.jsonl'; b=root/'b.jsonl'
            a.write_text(json.dumps(row)+'\n',encoding='utf-8')
            b.write_text(json.dumps(row)+'\n',encoding='utf-8')
            stats=collector.import_metadata([a,b])
            self.assertEqual(stats['unique_records_after'],1)
            self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM record_sources').fetchone()[0],2)
            source=root/'prior'; pdf_sha=hashlib.sha256(PDF).hexdigest()
            pdf_path=source/'raw/pdf'/pdf_sha[:2]/(pdf_sha+'.pdf')
            pdf_path.parent.mkdir(parents=True); pdf_path.write_bytes(PDF)
            manifest=dict(record_id='one',title='T',status='sucesso',limited=False,
                          files=[dict(path=pdf_path.relative_to(source).as_posix(),sha256=pdf_sha,
                                      bytes=len(PDF),url='https://repo/file.pdf',kind='pdf')],
                          attempts=[],metadata_from_html=[])
            (source/'raw/manifest_pdf.jsonl').write_text(json.dumps(manifest)+'\n',encoding='utf-8')
            metadata=(root/'target/metadata/records.jsonl').read_bytes()
            worker=Downloader(root/'target/pdfs',metadata,allow_growing_input=True)
            reused=collector.reuse_downloads(worker,[source])
            self.assertEqual(reused['candidate_files'],1)
            saved=json.loads(worker.db.execute('SELECT payload FROM results').fetchone()[0])
            self.assertTrue((worker.directory/saved['files'][0]['path']).exists())
            worker.db.close(); collector.db.close()

    def test_legacy_429_creates_restart_safe_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            log=Path(directory)/'logs/api_http.jsonl'; log.parent.mkdir(parents=True)
            log.write_text(json.dumps(dict(ts='2999-01-01T00:00:00+00:00',url=CONFIG['endpoint'],
                                           status=429,source='network'))+'\n',encoding='utf-8')
            collector.bootstrap_cooldown_from_log(3600)
            first=collector.active_cooldown(CONFIG['endpoint'])
            self.assertIsNotNone(first)
            collector.db.close()
            resumed=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            self.assertEqual(resumed.active_cooldown(CONFIG['endpoint'])['blocked_until'],
                             first['blocked_until'])
            resumed.db.close()

    def test_cache_success_does_not_clear_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            observed=datetime(2999,1,1,tzinfo=timezone.utc)
            collector.record_rate_limit(CONFIG['endpoint'],None,observed+timedelta(hours=1),
                                        'client_policy_no_retry_after',{},observed.isoformat())
            log=Path(directory)/'logs/api_http.jsonl'; log.parent.mkdir(parents=True)
            log.write_text(json.dumps(dict(ts=(observed+timedelta(minutes=8)).isoformat(),
                url=CONFIG['endpoint']+'?page=2',status=200,source='cache'))+'\n',encoding='utf-8')
            collector.bootstrap_cooldown_from_log(3600)
            self.assertIsNotNone(collector.active_cooldown(CONFIG['endpoint']))
            collector.db.close()

    def test_terminal_heartbeat_reports_real_snapshot(self):
        output=io.StringIO(); progress=TerminalProgress(interval=0.01)
        progress.queue=3; progress.attempted=2; progress.unique=1; progress.bytes=1024**2
        with redirect_stdout(output):
            progress.start(); time.sleep(0.025); progress.stop('teste')
        text=output.getvalue()
        self.assertIn('ATIVIDADE',text)
        self.assertIn('fila=3',text)
        self.assertIn('mb_pdf_raw=1.00',text)

    def test_filter_escaping(self):
        self.assertEqual(quoted_filter('institution','A"B'),'institution:"A\\"B"')
        self.assertEqual(quoted_filter('institution','X',True),'-institution:"X"')

    def test_plans_harvests_and_resumes_without_duplicate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            collector.plan_root(Fake())
            self.assertTrue(collector.harvest_one(Fake()))
            while collector.harvest_one(Fake()):
                if collector.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]>=120: break
            self.assertEqual(collector.db.execute('SELECT COUNT(*) FROM records').fetchone()[0],120)
            path,rows=collector.export_metadata()
            self.assertEqual(len(rows),120)
            self.assertEqual(len(path.read_text(encoding='utf-8').splitlines()),120)
            collector.db.close()

    def test_rate_limit_keeps_partition_pending_for_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            collector.plan_root(Fake())
            with self.assertRaises(urllib.error.HTTPError):
                collector.harvest_one(RateLimited())
            status=collector.db.execute("SELECT status FROM partitions ORDER BY priority LIMIT 1").fetchone()[0]
            self.assertEqual(status,'planned')
            collector.db.close()

    def test_validates_year_institution_complements_and_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            result=collector.validate_partitioning(ValidationFake())
            self.assertEqual(result['root_total'],120)
            self.assertTrue(result['residual_institution_complement']['matches'])
            self.assertEqual(result['no_year']['total'],5)
            self.assertTrue(result['other_year_values']['matches'])
            self.assertTrue(result['boundary']['valid'])
            collector.db.close()

    def test_run_stops_resumably_when_rate_limit_waits_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            with patch('bdtd.collect.Client',return_value=RateLimited()):
                result=collector.run('test@example.org',max_hours=1,max_rate_limit_waits=0)
            self.assertIn('rate_limit_429_em_validacao',result['stop_reason'])
            self.assertEqual(result['metadata_records'],0)
            collector.db.close()

    def test_max_hours_includes_and_bounds_rate_limit_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            clock=[0.0]; sleeps=[]
            def monotonic(): return clock[0]
            def sleep(seconds): sleeps.append(seconds); clock[0]+=seconds
            output=io.StringIO()
            with patch('bdtd.collect.Client',return_value=RateLimited()), \
                 patch('bdtd.collect.time.monotonic',side_effect=monotonic), \
                 patch('bdtd.collect.time.sleep',side_effect=sleep),redirect_stdout(output):
                result=collector.run('test@example.org',max_hours=0.01,
                                     rate_limit_wait_minutes=60,max_rate_limit_waits=1)
            self.assertEqual(result['stop_reason'],'limite_de_tempo_da_execucao_durante_espera_429')
            self.assertLessEqual(sum(sleeps),36.0)
            self.assertIn('retomada_prevista=',output.getvalue())
            collector.db.close()

    def test_existing_pdf_queue_is_drained_before_limited_api(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector(directory,CONFIG,target=5,reserve_bytes=0,batch_bytes=10)
            payload=dict(record_id='queued',title='Queued',urls=['https://repository.example/work.pdf'],
                         query_id=collector.signature)
            with collector.db:
                collector.db.execute('INSERT INTO records VALUES(?,?,?)',
                                     ('queued',json.dumps(payload),'now'))
            pdf_client=QueuePDFClient(directory)
            output=io.StringIO()
            with patch('bdtd.collect.Client',return_value=RateLimited()), \
                 patch('bdtd.collect.StreamClient',return_value=pdf_client),redirect_stdout(output):
                result=collector.run('test@example.org',max_hours=1,max_rate_limit_waits=0)
            self.assertEqual(result['downloads']['candidate_unique_pdfs'],1)
            self.assertEqual(pdf_client.calls,['https://repository.example/work.pdf'])
            self.assertIn('DOWNLOAD_INICIO',output.getvalue())
            self.assertIn('DOWNLOAD_FIM',output.getvalue())
            collector.db.close()


if __name__=='__main__': unittest.main()
