"""Piloto de PDFs. Download não equivale a validação semântica ou extração."""
import argparse
import collections
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from .core import atomic, digest, now
from .pdf_http import FetchError, StreamClient, clean_url

VERSION='0.3.3'
LIMIT_STATUSES={'limite_arquivo','limite_execucao','limite_profundidade',
                'redirect_limite','html_grande'}
COMPLETED_ATTEMPTS={'pdf_obtido','pdf_duplicado','links_localizados'}


def obvious_site_document(url):
    """Reconhece PDFs institucionais inequívocos que aparecem em menus/rodapés."""
    name=unquote(urlsplit(url).path.rsplit('/',1)[-1]).casefold()
    name=''.join(c for c in unicodedata.normalize('NFKD',name) if not unicodedata.combining(c))
    compact=re.sub(r'[^a-z0-9]+','-',name).strip('-')
    markers=('politica-de-informacao','politica-de-privacidade','privacy-policy',
             'termos-de-uso','terms-of-use','politica-do-repositorio')
    return any(marker in compact for marker in markers)


def candidate_identity(url):
    """Agrupa variantes conhecidas do mesmo bitstream sem alterar a URL requisitada."""
    p=urlsplit(url)
    path=re.sub(r'/+','/',p.path)
    path=re.sub(r';jsessionid=[^/;]+','',path,flags=re.IGNORECASE)
    dspace_path=path[6:] if path.lower().startswith('/xmlui/') else path
    handle=re.match(r'^/bitstream/handle/([^/]+/[^/]+)/([^/]+)$',dspace_path,re.IGNORECASE)
    legacy=re.match(r'^/bitstream/([^/]+/[^/]+)/\d+/([^/]+)$',dspace_path,re.IGNORECASE)
    match=handle or legacy
    if match:
        return ('dspace',p.scheme.lower(),p.netloc.lower(),match.group(1),match.group(2).casefold())
    return ('url',p.scheme.lower(),p.netloc.lower(),path,p.query)

class Links(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base=base
        self.candidates=[]
        self.filtered_candidates=[]
        self.identity_indexes={}
        self.metadata=collections.defaultdict(list)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='meta':
            name=(a.get('name') or a.get('property') or '').lower()
            value=a.get('content')
            if name and value and (name.startswith('citation_') or name.startswith('dc.') or name.startswith('dcterms.')):
                self.metadata[name].append(value)
            if name=='citation_pdf_url' and value: self.add(value,source='citation_pdf_url')
        if tag in ('a','link','iframe','embed','object'):
            value=a.get('href') or a.get('src') or a.get('data')
            if value:
                low=value.lower()
                if '.pdf' in low or '/bitstream/' in low or ('/bitstreams/' in low and '/download' in low) or a.get('type')=='application/pdf':
                    # Miniaturas/arquivos auxiliares de texto não são candidatos PDF.
                    if not any(urlsplit(low).path.endswith(x) for x in ('.jpg','.png','.gif','.txt','.xml','.zip')):
                        self.add(value,source=tag)
    def add(self, value, source=None):
        try: url=clean_url(urljoin(self.base,value))
        except (ValueError,FetchError): return
        if source!='citation_pdf_url' and obvious_site_document(url):
            self.filtered_candidates.append(dict(url=url,reason='documento_institucional_do_site'))
            return
        identity=candidate_identity(url)
        if identity in self.identity_indexes:
            index=self.identity_indexes[identity]
            # Prefere a forma sem barras duplicadas quando a página publica ambas.
            if '//' in urlsplit(self.candidates[index]).path and '//' not in urlsplit(url).path:
                self.candidates[index]=url
            return
        self.identity_indexes[identity]=len(self.candidates)
        self.candidates.append(url)


def pdf_signature(path):
    with Path(path).open('rb') as f:
        first=f.read(1024)
        f.seek(max(0,Path(path).stat().st_size-65536))
        end=f.read()
    return first.lstrip().startswith(b'%PDF-') and b'%%EOF' in end

class Downloader:
    def __init__(self, directory, input_bytes):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.input_sha=digest(input_bytes)
        self.db=sqlite3.connect(self.directory/'pdf_state.sqlite')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, sha TEXT);
        CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT, payload TEXT);
        ''')
        old=self.db.execute('SELECT sha FROM settings WHERE id=1').fetchone()
        if old and old[0]!=self.input_sha:
            self.db.close()
            raise ValueError('Entrada mudou. Use outro --data-dir para preservar o snapshot anterior.')
        with self.db: self.db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)',(self.input_sha,))
        source=self.directory/'raw/input'/ (self.input_sha+'.jsonl')
        source.parent.mkdir(parents=True,exist_ok=True)
        if not source.exists(): source.write_bytes(input_bytes)
        elif digest(source.read_bytes()) != self.input_sha: raise ValueError('Snapshot de entrada corrompido.')

    def obtain(self, client, url, refresh=False):
        cached=self.db.execute('SELECT payload FROM cache WHERE url=?',(url,)).fetchone()
        if cached:
            value=json.loads(cached[0])
            path=self.directory/value['path']
            if path.exists() and file_hash(path)==value['sha256'] and (not refresh or value['kind']=='pdf'):
                return dict(value,cached=True)
        result=client.fetch(url)
        path=Path(result['path'])
        signature=pdf_signature(path)
        ctype=result['content_type']
        kind='pdf' if signature else 'html' if ctype in ('text/html','application/xhtml+xml') else 'other'
        with path.open('rb') as f: start=f.read(1024).lstrip()
        if not signature and (start.startswith(b'%PDF-') or ctype=='application/pdf'):
            kind='invalid_pdf'
        extension={'pdf':'.pdf','html':'.html','other':'.bin','invalid_pdf':'.bin'}[kind]
        relative=Path('raw')/kind/result['sha256'][:2]/(result['sha256']+extension)
        dest=self.directory/relative
        dest.parent.mkdir(parents=True,exist_ok=True)
        recovered_corrupt_path=None
        if dest.exists() and file_hash(dest)!=result['sha256']:
            corrupt_sha=file_hash(dest)
            corrupt_dir=self.directory/'raw/corrupt'/corrupt_sha[:2]
            corrupt_dir.mkdir(parents=True,exist_ok=True)
            corrupt_dest=corrupt_dir/(corrupt_sha+dest.suffix)
            counter=1
            while corrupt_dest.exists():
                corrupt_dest=corrupt_dir/(f'{corrupt_sha}.{counter}{dest.suffix}')
                counter+=1
            os.replace(dest,corrupt_dest)
            recovered_corrupt_path=corrupt_dest.relative_to(self.directory).as_posix()
        os.replace(path,dest)
        value=dict(result,path=relative.as_posix(),kind=kind,cached=False,harvest_ts=now())
        if recovered_corrupt_path: value['recovered_corrupt_path']=recovered_corrupt_path
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)',(url,json.dumps(value,ensure_ascii=False)))
        return value

    def process(self, row, client, retry=False):
        rid=row['record_id']
        trace=[]
        files={}
        extracted=[]
        prior=self.db.execute('SELECT payload FROM results WHERE id=?',(rid,)).fetchone()
        if prior:
            previous=json.loads(prior[0])
            for item in previous['files']:
                path=self.directory/item['path']
                if path.exists() and file_hash(path)==item['sha256']:
                    files[item['sha256']]=item
            extracted=list(previous['metadata_from_html'])
        def already_obtained(url):
            cached=self.db.execute('SELECT payload FROM cache WHERE url=?',(url,)).fetchone()
            return cached is not None and json.loads(cached[0])['sha256'] in files
        visited=set()
        queue=[(url,0) for url in dict.fromkeys(row.get('urls',[])) if not already_obtained(url)]
        limited=False
        while queue and len(visited)<12:
            url,depth=queue.pop(0)
            if url in visited: continue
            visited.add(url)
            event=dict(url=url,at=now(),depth=depth,pipeline_version=VERSION,
                       input_sha256=self.input_sha)
            if getattr(client,'run_id',None): event['run_id']=client.run_id
            try:
                result=self.obtain(client,url,refresh=retry)
                event.update(final_url=result['url'],kind=result['kind'],sha256=result['sha256'],
                             bytes=result['bytes'],cached=result['cached'])
                if result['kind']=='pdf':
                    duplicate=result['sha256'] in files
                    files.setdefault(result['sha256'],dict(result,validation='assinatura_e_EOF',document_role='nao_verificado'))
                    event['status']='pdf_duplicado' if duplicate else 'pdf_obtido'
                elif result['kind']=='html':
                    raw=(self.directory/result['path']).read_bytes()
                    if len(raw)>10*1024**2: raise FetchError('html_grande','Página HTML excede 10 MiB para parsing.')
                    text=raw.decode(result.get('charset','utf-8'),errors='replace')
                    lowered=text.lower()
                    if any(x in lowered for x in ('verificando seu navegador','verify you are human','checking your browser','cf-chl-','g-recaptcha')):
                        raise FetchError('verificacao_navegador','Página exige verificação; não foi contornada.')
                    parser=Links(result['url'])
                    parser.feed(text)
                    metadata=dict(source_url=result['url'],sha256=result['sha256'],fields=dict(parser.metadata))
                    if metadata not in extracted: extracted.append(metadata)
                    candidates=[u for u in parser.candidates if u not in visited and not already_obtained(u)]
                    event['candidates_found']=len(candidates)
                    if parser.filtered_candidates:
                        event['filtered_candidates']=parser.filtered_candidates
                    if candidates and depth==0:
                        queue.extend((u,1) for u in candidates[:8])
                        limited=limited or len(candidates)>8
                        event['status']='links_localizados'
                    else:
                        event['status']=('links_localizados' if parser.candidates and not candidates else
                                         'sem_link_pdf_no_html' if not candidates else 'limite_profundidade')
                elif result['kind']=='invalid_pdf':
                    event['status']='pdf_invalido_ou_incompleto'
                else: event['status']='formato_nao_suportado'
            except Exception as exc:
                event.update(status=getattr(exc,'code','erro_rede_ou_processamento'),reason=str(exc))
                if getattr(exc,'details',None): event['details']=dict(exc.details)
            limited=limited or event['status'] in LIMIT_STATUSES
            trace.append(event)
            with self.db:
                self.db.execute('INSERT INTO attempts(record_id,payload) VALUES(?,?)',
                                (rid,json.dumps(event,ensure_ascii=False)))
            if event['status']=='limite_execucao':
                limited=True
                break
        limited=limited or bool(queue)
        status='sucesso' if files else 'sem_link_origem' if not row.get('urls') else 'nao_obtido'
        result=dict(record_id=rid,title=row.get('title'),status=status,files=list(files.values()),
                    attempts=trace,metadata_from_html=extracted,input_sha256=self.input_sha,
                    query_id=row.get('query_id'),pipeline_version=VERSION,finished_at=now(),
                    limited=limited,all_attachments_confirmed=False)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO results VALUES(?,?)',(rid,json.dumps(result,ensure_ascii=False)))
        return result

    def export(self, total_input):
        rows=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM results ORDER BY id')]
        atomic(self.directory/'raw/manifest_pdf.jsonl',''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
        metadata=[dict(record_id=r['record_id'],**m) for r in rows for m in r['metadata_from_html']]
        atomic(self.directory/'staging/metadata_html.jsonl',''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in metadata))
        reasons=collections.Counter(a['status'] for r in rows for a in r['attempts'])
        unique={f['sha256']:f['bytes'] for r in rows for f in r['files']}
        site_documents={f['sha256']:f['bytes'] for r in rows for f in r['files']
                        if obvious_site_document(f.get('url',''))}
        candidate_work_files={f['sha256']:f['bytes'] for r in rows for f in r['files']
                              if not obvious_site_document(f.get('url',''))}
        pdf_events=[a for r in rows for a in r['attempts']
                    if a['status'] in ('pdf_obtido','pdf_duplicado')]
        seen_pdf=set()
        duplicate_events=[]
        for event in pdf_events:
            if event.get('sha256') in seen_pdf or event['status']=='pdf_duplicado':
                duplicate_events.append(event)
            seen_pdf.add(event.get('sha256'))
        duplicate_bytes=sum(a.get('bytes',unique.get(a.get('sha256'),0)) for a in duplicate_events)
        run_ids=collections.Counter(a['run_id'] for r in rows for a in r['attempts'] if a.get('run_id'))
        domains=collections.defaultdict(collections.Counter)
        for r in rows:
            if r['attempts']: domains[urlsplit(r['attempts'][0]['url']).netloc][r['status']]+=1
        stages=collections.Counter(a.get('details',{}).get('stage','nao_registrada')
                                   for r in rows for a in r['attempts']
                                   if a['status'] not in COMPLETED_ATTEMPTS)
        report=dict(version=VERSION,input_sha256=self.input_sha,
                    result_versions=dict(collections.Counter(r['pipeline_version'] for r in rows)),
                    input_records=total_input,processed_records=len(rows),
                    remaining_records=total_input-len(rows),statuses=dict(collections.Counter(r['status'] for r in rows)),
                    unique_pdfs=len(unique),pdf_bytes=sum(unique.values()),attempt_statuses=dict(reasons),
                    candidate_work_pdfs=len(candidate_work_files),
                    site_document_pdfs=len(site_documents),
                    possible_extra_candidate_files=sum(max(0,sum(not obvious_site_document(f.get('url',''))
                                                                  for f in r['files'])-1) for r in rows),
                    pdf_responses=len(pdf_events),duplicate_pdf_responses=len(duplicate_events),
                    duplicate_pdf_bytes=duplicate_bytes,
                    run_ids=dict(run_ids),
                    failure_stages=dict(stages),
                    by_initial_domain={d:dict(c) for d,c in domains.items()},
                    semantic_validation=False,text_extraction=False,area_validated=False,
                    results=[dict(record_id=r['record_id'],status=r['status'],pdfs=len(r['files']),
                                  candidate_work_pdfs=sum(not obvious_site_document(f.get('url','')) for f in r['files']),
                                  site_document_pdfs=sum(obvious_site_document(f.get('url','')) for f in r['files']),
                                  pipeline_version=r['pipeline_version'],limited=r['limited'],
                                  reasons=[a.get('reason',a['status']) for a in r['attempts'] if a['status'] not in COMPLETED_ATTEMPTS],
                                  attempts=[{k:a[k] for k in ('url','final_url','status','reason','details','sha256','bytes','cached','run_id') if k in a}
                                            for a in r['attempts']]) for r in rows])
        atomic(self.directory/'reports/pdf_pilot.json',json.dumps(report,ensure_ascii=False,indent=2))
        return report


def file_hash(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def load_rows(data):
    rows=[json.loads(line) for line in data.decode('utf-8-sig').splitlines() if line.strip()]
    ids=set()
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('record_id'),str) or not row['record_id']:
            raise ValueError('Cada linha precisa de record_id.')
        if row['record_id'] in ids: raise ValueError('ID repetido na entrada.')
        ids.add(row['record_id'])
        if not isinstance(row.get('urls'),list) or not all(isinstance(u,str) for u in row['urls']):
            raise ValueError('urls deve ser lista de strings.')
    return rows

def main():
    parser=argparse.ArgumentParser(description='Piloto de download de PDFs BDTD')
    parser.add_argument('--input',type=Path,default=Path('examples/piloto_40_metadados.jsonl'))
    parser.add_argument('--data-dir',type=Path,default=Path('data/pdf-piloto'))
    parser.add_argument('--contact',required=True)
    parser.add_argument('--limit',type=int,default=8,help='Máximo de registros tentados nesta execução')
    parser.add_argument('--max-mb',type=int,default=100)
    parser.add_argument('--budget-mb',type=int,default=500)
    parser.add_argument('--retry-failed',action='store_true',help='Inclui falhas e sucessos parciais; páginas HTML são atualizadas')
    args=parser.parse_args()
    if not 1<=args.limit<=50 or args.max_mb<1 or args.budget_mb<1: parser.error('Limite: 1–50 registros; MB devem ser positivos.')
    if '@' not in args.contact or any(c.isspace() for c in args.contact): parser.error('Informe um e-mail válido.')
    worker=None
    try:
        data=args.input.read_bytes()
        rows=load_rows(data)
        worker=Downloader(args.data_dir,data)
        client=StreamClient(args.contact,args.data_dir,args.max_mb*1024**2,args.budget_mb*1024**2)
        attempted=0
        for row in rows:
            prior=worker.db.execute('SELECT payload FROM results WHERE id=?',(row['record_id'],)).fetchone()
            if prior:
                old=json.loads(prior[0])
                if old['status']=='sucesso':
                    valid_files=all((args.data_dir/f['path']).exists() and file_hash(args.data_dir/f['path'])==f['sha256'] for f in old['files'])
                    incomplete=old['limited'] or any(a['status'] not in COMPLETED_ATTEMPTS for a in old['attempts'])
                    if valid_files and not (args.retry_failed and incomplete): continue
                elif not args.retry_failed: continue
            if attempted>=args.limit or client.transferred>=client.budget: break
            print('Processando:',row['record_id'],flush=True)
            client.record_id=row['record_id']
            try:
                result=worker.process(row,client,retry=args.retry_failed)
            finally:
                client.record_id=None
            attempted+=1
            worker.export(len(rows))
            print(result['status'], '-',len(result['files']),'PDF(s)',flush=True)
            if client.transferred>=client.budget or any(a['status']=='limite_execucao' for a in result['attempts']): break
        report=worker.export(len(rows))
        print('PDFs únicos:',report['unique_pdfs'])
        print('Relatório:',(args.data_dir/'reports/pdf_pilot.json').resolve())
    except KeyboardInterrupt:
        if worker: worker.export(len(rows))
        parser.exit(130,'Interrompido. Resultados concluídos preservados; execute novamente para retomar.\n')
    except Exception as exc:
        parser.exit(1,f'Interrompido: {exc}\n')
    finally:
        if worker: worker.db.close()

if __name__=='__main__': main()
