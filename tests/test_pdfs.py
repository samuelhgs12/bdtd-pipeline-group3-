import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from bdtd.pdfs import Downloader, Links, load_rows, pdf_signature, main as pdf_main
from bdtd.pdf_http import StreamClient, FetchError

PDF=b'%PDF-1.4\nsynthetic fixture, not a real thesis\n%%EOF\n'

class Fake:
    def __init__(self,directory,responses): self.dir=Path(directory); self.responses=responses; self.calls=[]
    def fetch(self,url):
        self.calls.append(url)
        value=self.responses[url]
        if isinstance(value,Exception): raise value
        body,kind=value
        path=self.dir/('tmp-'+str(len(self.calls)))
        path.write_bytes(body)
        return dict(path=str(path),sha256=hashlib.sha256(body).hexdigest(),bytes=len(body),
                    content_type=kind,charset='utf-8',url=url)

class PDFTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.worker=Downloader(self.tmp.name,b'input-fixture')
    def tearDown(self):
        self.worker.db.close(); self.tmp.cleanup()
    def test_html_candidates_and_metadata(self):
        p=Links('https://example.org/handle/1')
        p.feed('<meta name="citation_pdf_url" content="/file.pdf"><meta name="DC.description" content="Resumo"><a href="/bitstream/1/main.pdf?sequence=1">PDF</a><a href="/thumb.jpg">Img</a><a href="/bitstream/1/file.txt">TXT</a>')
        self.assertEqual(len(p.candidates),2)
        self.assertEqual(p.metadata['dc.description'],['Resumo'])
    def test_html_download_cache_and_record_mapping(self):
        page='https://example.org/handle/1'; pdf='https://example.org/main.pdf'
        client=Fake(self.tmp.name,{page:(b'<meta name="citation_pdf_url" content="/main.pdf">','text/html'),pdf:(PDF,'application/pdf')})
        row=dict(record_id='a',urls=[page])
        first=self.worker.process(row,client)
        second=self.worker.process(dict(row,record_id='b'),client)
        self.assertEqual(first['status'],'sucesso')
        self.assertEqual(second['status'],'sucesso')
        self.assertEqual(len(client.calls),2)
        report=self.worker.export(2)
        self.assertEqual(report['unique_pdfs'],1)
        self.assertEqual(report['statuses']['sucesso'],2)
    def test_no_link_and_404(self):
        client=Fake(self.tmp.name,{'https://example.org/dead':FetchError('http_404','HTTP 404')})
        self.assertEqual(self.worker.process(dict(record_id='a',urls=[]),client)['status'],'sem_link_origem')
        bad=self.worker.process(dict(record_id='b',urls=['https://example.org/dead']),client)
        self.assertEqual(bad['status'],'nao_obtido')
        self.assertEqual(bad['attempts'][0]['status'],'http_404')
    def test_pdf_extension_with_html_not_success(self):
        url='https://example.org/not.pdf'
        client=Fake(self.tmp.name,{url:(b'<html>Login</html>','text/html')})
        self.assertEqual(self.worker.process(dict(record_id='a',urls=[url]),client)['status'],'nao_obtido')
    def test_truncated_pdf_not_success(self):
        url='https://example.org/file.pdf'
        client=Fake(self.tmp.name,{url:(b'%PDF-1.7\nincomplete','application/pdf')})
        r=self.worker.process(dict(record_id='a',urls=[url]),client)
        self.assertEqual(r['attempts'][0]['status'],'pdf_invalido_ou_incompleto')
    def test_retry_recovers_failed_record(self):
        url='https://example.org/file.pdf'
        client=Fake(self.tmp.name,{url:FetchError('http_503','503')})
        self.worker.process(dict(record_id='a',urls=[url]),client)
        client.responses[url]=(PDF,'application/pdf')
        self.assertEqual(self.worker.process(dict(record_id='a',urls=[url]),client,retry=True)['status'],'sucesso')
    def test_input_validation_and_snapshot(self):
        with self.assertRaises(ValueError): load_rows(b'{"record_id":"a","urls":"bad"}')
        with self.assertRaises(ValueError): Downloader(self.tmp.name,b'different')
    def test_multiple_attachments(self):
        page='https://example.org/item'
        client=Fake(self.tmp.name,{page:(b'<a href="/a.pdf">a</a><a href="/b.pdf">b</a>','text/html'),
            'https://example.org/a.pdf':(PDF,'application/pdf'),'https://example.org/b.pdf':(PDF.replace(b'fixture',b'appendix'),'application/pdf')})
        r=self.worker.process(dict(record_id='a',urls=[page]),client)
        self.assertEqual(len(r['files']),2)
        self.assertFalse(r['all_attachments_confirmed'])

    def test_resource_limits_mark_record_limited(self):
        url='https://example.org/large.pdf'
        for status in ('limite_arquivo','limite_execucao','redirect_limite','html_grande'):
            with self.subTest(status=status):
                client=Fake(self.tmp.name,{url:FetchError(status,'limite')})
                result=self.worker.process(dict(record_id=status,urls=[url]),client)
                self.assertTrue(result['limited'])
                self.assertEqual(result['attempts'][0]['status'],status)

    def test_discovery_depth_limit_is_reported(self):
        page='https://example.org/item'; second='https://example.org/next.pdf'
        client=Fake(self.tmp.name,{page:(b'<a href="/next.pdf">PDF</a>','text/html'),
                                  second:(b'<a href="/actual.pdf">PDF</a>','text/html')})
        result=self.worker.process(dict(record_id='a',urls=[page]),client)
        self.assertTrue(result['limited'])
        self.assertEqual(result['attempts'][-1]['status'],'limite_profundidade')
        self.assertEqual(client.calls,[page,second])

    def test_diagnostics_and_legacy_results_keep_their_provenance(self):
        url='https://example.org/a.pdf'
        details=dict(stage='robots',request_url='https://example.org/robots.txt',
                     http_status=None,cause='timeout',policy_cached=False)
        client=Fake(self.tmp.name,{url:FetchError('robots_indisponivel','timed out',details)})
        result=self.worker.process(dict(record_id='new',urls=[url]),client)
        legacy=dict(result,record_id='old',pipeline_version='0.3.0',attempts=[
            dict(url=url,status='erro_rede_ou_processamento',reason='timed out')])
        legacy_payload=json.dumps(legacy)
        with self.worker.db:
            self.worker.db.execute('INSERT INTO results VALUES(?,?)',('old',legacy_payload))
        report=self.worker.export(2)
        self.assertEqual(report['input_sha256'],self.worker.input_sha)
        self.assertEqual(report['result_versions'],{'0.3.1':1,'0.3.0':1})
        self.assertEqual(report['failure_stages'],{'robots':1,'nao_registrada':1})
        self.assertEqual(report['results'][0]['attempts'][0]['details'],details)
        self.assertEqual(self.worker.db.execute('SELECT payload FROM results WHERE id=?',('old',)).fetchone()[0],legacy_payload)
        history=json.loads(self.worker.db.execute('SELECT payload FROM attempts').fetchone()[0])
        self.assertEqual(history['details'],details)
        self.assertEqual(history['pipeline_version'],'0.3.1')

    def test_failed_retry_keeps_previous_pdf_metadata_and_attempts(self):
        page='https://example.org/item'; pdf='https://example.org/main.pdf'
        client=Fake(self.tmp.name,{page:(b'<meta name="dc.title" content="Title"><a href="/main.pdf">PDF</a>','text/html'),pdf:(PDF,'application/pdf')})
        row=dict(record_id='a',urls=[page])
        first=self.worker.process(row,client)
        history=self.worker.db.execute('SELECT id,payload FROM attempts ORDER BY id').fetchall()
        raw_path=Path(self.tmp.name)/first['files'][0]['path']
        client.responses[page]=FetchError('robots_indisponivel','HTTP 403 no robots')
        second=self.worker.process(row,client,retry=True)
        self.assertEqual(second['status'],'sucesso')
        self.assertEqual(second['files'],first['files'])
        self.assertEqual(second['metadata_from_html'],first['metadata_from_html'])
        self.assertEqual(raw_path.read_bytes(),PDF)
        self.assertEqual(self.worker.db.execute('SELECT id,payload FROM attempts ORDER BY id').fetchall()[:2],history)
        self.assertEqual(second['attempts'][0]['status'],'robots_indisponivel')


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/'input.jsonl'
        self.directory=self.root/'output'
        self.client=Fake(self.root,{})
        self.client.transferred=0
        self.client.budget=1024**2

    def run_cli(self,rows,*args):
        if not self.source.exists():
            self.source.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        argv=['bdtd.pdfs','--input',str(self.source),'--data-dir',str(self.directory),
              '--contact','test@example.org',*args]
        with patch('sys.argv',argv),patch('bdtd.pdfs.StreamClient',return_value=self.client),redirect_stdout(io.StringIO()):
            pdf_main()
        return json.loads((self.directory/'reports/pdf_pilot.json').read_text(encoding='utf-8'))

    def test_default_eight_records_resume_without_repeating_failures(self):
        rows=[dict(record_id=str(i),urls=[f'https://example.org/{i}.pdf']) for i in range(10)]
        self.client.responses={r['urls'][0]:FetchError('robots_indisponivel','unavailable') for r in rows}
        first=self.run_cli(rows)
        self.assertEqual(first['processed_records'],8)
        self.assertEqual(first['remaining_records'],2)
        self.assertEqual(len(self.client.calls),8)
        second=self.run_cli(rows)
        self.assertEqual(second['processed_records'],10)
        self.assertEqual(len(self.client.calls),10)
        self.run_cli(rows)
        self.assertEqual(len(self.client.calls),10)
        with sqlite3.connect(self.directory/'pdf_state.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],10)

    def test_explicit_retry_finishes_partial_success_using_valid_cached_pdf(self):
        page='https://example.org/item'; a='https://example.org/a.pdf'; b='https://example.org/b.pdf'
        rows=[dict(record_id='a',urls=[page])]
        self.client.responses={page:(b'<a href="/a.pdf">A</a><a href="/b.pdf">B</a>','text/html'),
                               a:(PDF,'application/pdf'),b:FetchError('http_503','503')}
        first=self.run_cli(rows)
        self.assertEqual(first['unique_pdfs'],1)
        with sqlite3.connect(self.directory/'pdf_state.sqlite') as db:
            history=db.execute('SELECT id,payload FROM attempts ORDER BY id').fetchall()
        self.run_cli(rows)
        self.assertEqual(self.client.calls,[page,a,b])
        self.client.responses[b]=(PDF.replace(b'fixture',b'appendix'),'application/pdf')
        second=self.run_cli(rows,'--retry-failed')
        self.assertEqual(second['unique_pdfs'],2)
        self.assertEqual(self.client.calls,[page,a,b,page,b])
        with sqlite3.connect(self.directory/'pdf_state.sqlite') as db:
            self.assertEqual(db.execute('SELECT id,payload FROM attempts ORDER BY id').fetchall()[:3],history)
        self.run_cli(rows,'--retry-failed')
        self.assertEqual(self.client.calls,[page,a,b,page,b])

    def test_exact_budget_stops_before_marking_next_record_attempted(self):
        urls=['https://example.org/a.pdf','https://example.org/b.pdf']
        rows=[dict(record_id=str(i),urls=[url]) for i,url in enumerate(urls)]
        self.client.responses={url:(PDF,'application/pdf') for url in urls}
        fetch=self.client.fetch
        def consume_budget(url):
            self.client.transferred=self.client.budget
            return fetch(url)
        with patch.object(self.client,'fetch',side_effect=consume_budget):
            report=self.run_cli(rows)
        self.assertEqual(self.client.calls,urls[:1])
        self.assertEqual(report['processed_records'],1)
        self.assertEqual(report['remaining_records'],1)

    def test_retry_advances_past_eight_previously_downloaded_candidates(self):
        page='https://example.org/item'
        rows=[dict(record_id='a',urls=[page])]
        html=''.join(f'<a href="/{i}.pdf">PDF</a>' for i in range(9)).encode()
        self.client.responses={page:(html,'text/html')}
        self.client.responses.update({f'https://example.org/{i}.pdf':
            (PDF.replace(b'fixture',str(i).encode()),'application/pdf') for i in range(9)})
        first=self.run_cli(rows)
        self.assertEqual(first['unique_pdfs'],8)
        self.assertTrue(first['results'][0]['limited'])
        second=self.run_cli(rows,'--retry-failed')
        self.assertEqual(second['unique_pdfs'],9)
        self.assertFalse(second['results'][0]['limited'])
        self.assertEqual(self.client.calls[-2:],[page,'https://example.org/8.pdf'])
        self.assertEqual(len(self.client.calls),11)
        metadata=(self.directory/'staging/metadata_html.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(metadata),1)

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.client=StreamClient('test@example.org',self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def test_redirect_checks_policy_each_hop(self):
        headers=Message(); headers['Location']='https://two.example/file.pdf'
        error=HTTPError('https://one.example/id',302,'redirect',headers,None)
        with patch.object(self.client,'_policy') as policy, patch.object(self.client,'_transfer',side_effect=[error,{'ok':True}]):
            self.assertEqual(self.client.fetch('https://one.example/id'),{'ok':True})
            self.assertEqual([x.args[0] for x in policy.call_args_list],['https://one.example/id','https://two.example/file.pdf'])
    def test_redirect_target_robots_blocked(self):
        headers=Message(); headers['Location']='https://two.example/file.pdf'
        error=HTTPError('https://one.example/id',302,'redirect',headers,None)
        with patch.object(self.client,'_policy',side_effect=[None,FetchError('robots_bloqueado','blocked')]), patch.object(self.client,'_transfer',side_effect=error) as transfer:
            with self.assertRaises(FetchError): self.client.fetch('https://one.example/id')
            self.assertEqual(transfer.call_count,1)
    def test_stream_limit_removes_partial(self):
        class Response:
            status=200
            headers=Message()
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n): return b'x'*n
        with patch.object(self.client.opener,'open',return_value=Response()):
            with self.assertRaises(FetchError): self.client._once('https://example.org/f',100)
        self.assertEqual(list((Path(self.tmp.name)/'tmp').iterdir()),[])
    def test_robots_denies(self):
        path=Path(self.tmp.name)/'robots.tmp'
        body=b'User-agent: *\nDisallow: /blocked\n'
        path.write_bytes(body)
        result=dict(path=str(path),sha256=hashlib.sha256(body).hexdigest())
        with patch.object(self.client,'_transfer',return_value=result):
            with self.assertRaises(FetchError): self.client._policy('https://example.org/blocked')
