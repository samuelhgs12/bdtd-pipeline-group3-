"""Coleta incremental particionada de metadados e PDFs da consulta BDTD validada."""
import argparse
import collections
import json
import math
import os
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import build_url, normalize, strings, validate
from .batches import BatchBuilder
from .core import atomic, digest, now
from .network import Client
from .pdf_http import StreamClient
from .pdfs import COMPLETED_ATTEMPTS, Downloader, file_hash, load_rows, obvious_site_document

VERSION='0.4.1'
QUERY_CONTRACT_VERSION='0.4.0'
WINDOW=1000
PAGE_LIMIT=100
PARTITION_STRATEGY='year-and-global-institution-complements-v2'
YEAR_CRITERION_PREFIX='__publishDate_year__:'
ANNUAL_WINDOW_PREFIX='janela-anual-'
ANNUAL_PAGE_LIMIT=20
ANNUAL_MAX_PAGES=50


class RateLimitStop(Exception):
    pass


class TerminalProgress:
    """Emite somente estado observado; o heartbeat não inventa avanço."""
    def __init__(self,interval=30):
        self.interval=interval
        self.lock=threading.RLock()
        self.stopped=threading.Event()
        self.thread=None
        self.stage='inicializacao'
        self.wait_reason=None
        self.resume_at=None
        self.queue=0
        self.attempted=0
        self.works=0
        self.unique=0
        self.bytes=0
        self.failures={}

    def _summary(self,prefix):
        wait=''
        if self.wait_reason:
            wait=f' espera={self.wait_reason} retomada_prevista={self.resume_at}'
        return (f'{prefix} etapa={self.stage} fila={self.queue} tentados={self.attempted} '
                f'trabalhos_pdf={self.works} pdfs_unicos={self.unique} '
                f'mb_pdf_raw={self.bytes/1024**2:.2f} falhas={json.dumps(self.failures,ensure_ascii=False)}{wait}')

    def emit(self,prefix='ATIVIDADE'):
        with self.lock:
            print(self._summary(prefix),flush=True)

    def start(self):
        self.emit('COLETA_INICIO')
        self.thread=threading.Thread(target=self._heartbeat,name='bdtd-progress',daemon=True)
        self.thread.start()

    def stop(self,reason):
        self.stopped.set()
        if self.thread: self.thread.join(timeout=1)
        self.wait_reason=None; self.resume_at=None
        self.emit('COLETA_FIM motivo='+str(reason))

    def _heartbeat(self):
        while not self.stopped.wait(self.interval):
            self.emit('ATIVIDADE')

    def stage_start(self,name):
        self.stage=name
        self.emit('ETAPA_INICIO')

    def stage_end(self,name):
        self.stage=name
        self.emit('ETAPA_FIM')

    def update_collection(self,report,emit=False):
        downloads=report.get('downloads',{})
        self.queue=max(0,report.get('metadata_records',0)-downloads.get('records_attempted',0))
        self.attempted=downloads.get('records_attempted',0)
        self.works=downloads.get('works_with_pdf',0)
        self.unique=downloads.get('candidate_unique_pdfs',0)
        self.bytes=downloads.get('bytes',0)
        self.failures=downloads.get('failures',{})
        if emit: self.emit('PROGRESSO')

    def update_pdf(self,report,failures,emit=True):
        self.queue=max(0,report.get('remaining_records',0))
        self.attempted=report.get('processed_records',0)
        self.works=report.get('works_with_candidate_pdf',0)
        self.unique=report.get('candidate_work_pdfs',0)
        self.bytes=report.get('pdf_bytes',0)
        self.failures=dict(failures)
        if emit: self.emit('PROGRESSO')

    def waiting(self,reason,resume_at):
        self.stage='aguardando_api'
        self.wait_reason=reason
        self.resume_at=resume_at.astimezone().isoformat(timespec='seconds')
        self.emit('ESPERA_INICIO')

    def clear_wait(self):
        if self.wait_reason:
            self.emit('ESPERA_FIM')
        self.wait_reason=None; self.resume_at=None

    def download_event(self,kind,event):
        if kind=='start':
            print(f'DOWNLOAD_INICIO record_id={event["record_id"]} url={event["url"]}',flush=True)
        else:
            print(f'DOWNLOAD_FIM record_id={event["record_id"]} status={event.get("status")} '
                  f'bytes={event.get("bytes",0)} url={event.get("final_url",event["url"])}',flush=True)


def quoted_filter(field,value,negative=False):
    escaped=str(value).replace('\\','\\\\').replace('"','\\"')
    return ('-' if negative else '')+f'{field}:"{escaped}"'


def year_criterion(year):
    return YEAR_CRITERION_PREFIX+str(year)


def request_criteria(filters):
    """Converte o critério interno de ano no daterange aceito pela API."""
    api_filters=[]; years=[]
    for value in filters:
        if value.startswith(YEAR_CRITERION_PREFIX):
            years.append(value[len(YEAR_CRITERION_PREFIX):])
        elif value.startswith('publishDate:"') and value.endswith('"'):
            # Preserva checkpoints antigos, sem reenviar o filtro anual defeituoso.
            years.append(value[len('publishDate:"'):-1])
        else:
            api_filters.append(value)
    if len(set(years))>1:
        raise ValueError('Uma requisição não pode combinar anos distintos.')
    return api_filters,(years[0] if years else None)


def rate_limit_policy(exc,fallback_minutes=60):
    evidence=getattr(exc,'bdtd_evidence',{})
    raw=(evidence.get('retry_after') or exc.headers.get('Retry-After','')).strip()
    wait_source='server_retry_after'
    try:
        wait_seconds=float(raw) if raw else fallback_minutes*60
    except ValueError:
        try: wait_seconds=max(0,parsedate_to_datetime(raw).timestamp()-time.time())
        except (ValueError,TypeError,OverflowError):
            wait_seconds=fallback_minutes*60
            wait_source='client_policy_invalid_retry_after'
    if not raw: wait_source='client_policy_no_retry_after'
    return raw,max(0,wait_seconds),wait_source,evidence


def fingerprint(records):
    return digest(json.dumps([row['id'] for row in records],ensure_ascii=False).encode())


class Collector:
    def __init__(self,directory,config,target=5000,reserve_bytes=10*1024**3,
                 batch_bytes=1024**3):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        self.config=dict(config)
        self.config.pop('facets',None)
        self.target=target
        self.reserve_bytes=reserve_bytes
        self.batch_bytes=batch_bytes
        query_contract=dict(config=self.config,window=WINDOW,page_limit=PAGE_LIMIT,
                            version=QUERY_CONTRACT_VERSION,
                            partition_strategy=PARTITION_STRATEGY)
        self.signature=digest(json.dumps(query_contract,sort_keys=True,ensure_ascii=False).encode())
        self.db=sqlite3.connect(self.directory/'collection.sqlite')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY,signature TEXT,root_total INTEGER,
            created_at TEXT);
          CREATE TABLE IF NOT EXISTS partitions (id TEXT PRIMARY KEY,label TEXT,filters TEXT,parent_id TEXT,
            priority INTEGER,status TEXT,total INTEGER,next_page INTEGER,expected_total INTEGER,
            inaccessible_records INTEGER,error TEXT,created_at TEXT,updated_at TEXT);
          CREATE TABLE IF NOT EXISTS pages (partition_id TEXT,page INTEGER,total INTEGER,count INTEGER,
            sha TEXT,fingerprint TEXT,url TEXT,harvest_ts TEXT,
            PRIMARY KEY(partition_id,page));
          CREATE TABLE IF NOT EXISTS records (record_id TEXT PRIMARY KEY,payload TEXT,discovered_at TEXT);
          CREATE TABLE IF NOT EXISTS record_partitions (record_id TEXT,partition_id TEXT,page INTEGER,
            PRIMARY KEY(record_id,partition_id));
          CREATE TABLE IF NOT EXISTS conflicts (record_id TEXT,partition_id TEXT,page INTEGER,payload TEXT,
            detected_at TEXT,PRIMARY KEY(record_id,partition_id,page));
          CREATE TABLE IF NOT EXISTS validations (name TEXT PRIMARY KEY,payload TEXT,validated_at TEXT);
          CREATE TABLE IF NOT EXISTS api_cache (url TEXT PRIMARY KEY,sha TEXT,payload BLOB,harvest_ts TEXT);
          CREATE TABLE IF NOT EXISTS facet_values (field TEXT,value TEXT,count INTEGER,
            discovered_at TEXT,PRIMARY KEY(field,value));
          CREATE TABLE IF NOT EXISTS record_sources (record_id TEXT,source_path TEXT,
            source_query_id TEXT,compatibility_basis TEXT,source_sha TEXT,payload TEXT,imported_at TEXT,
            PRIMARY KEY(record_id,source_path));
          CREATE TABLE IF NOT EXISTS api_rate_limits (id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT,url TEXT,status INTEGER,retry_after TEXT,blocked_until TEXT,
            wait_source TEXT,evidence TEXT);
          CREATE TABLE IF NOT EXISTS api_cooldowns (origin TEXT PRIMARY KEY,blocked_until TEXT,
            observed_at TEXT,url TEXT,wait_source TEXT,event_id INTEGER);
          CREATE TABLE IF NOT EXISTS metadata_rejections (partition_id TEXT,page INTEGER,position INTEGER,
            record_id TEXT,reason TEXT,payload TEXT,raw_sha TEXT,detected_at TEXT,
            PRIMARY KEY(partition_id,page,position));
          CREATE TABLE IF NOT EXISTS api_invalid_responses (url TEXT,sha TEXT,observed_at TEXT,
            category TEXT,error TEXT,status_value TEXT,result_count_type TEXT,result_count_value TEXT,
            records_present INTEGER,records_type TEXT,PRIMARY KEY(url,sha));
        ''')
        previous=self.db.execute('SELECT signature FROM settings WHERE id=1').fetchone()
        if previous and previous[0]!=self.signature:
            has_work=(self.db.execute('SELECT COUNT(*) FROM partitions').fetchone()[0] or
                      self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0] or
                      self.db.execute('SELECT COUNT(*) FROM validations').fetchone()[0])
            if has_work:
                self.db.close()
                raise ValueError('A consulta mudou. Use outro --data-dir para preservar a coleta anterior.')
            with self.db:
                self.db.execute('UPDATE settings SET signature=? WHERE id=1',(self.signature,))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?,NULL,?)',(1,self.signature,now()))
        self.last_api_source=None

    def archive(self,data):
        sha=digest(data); path=self.directory/'raw/api'/f'{sha}.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and digest(path.read_bytes())!=sha:
            raise ValueError('Raw da API corrompido: '+str(path))
        if not path.exists(): path.write_bytes(data)
        return sha

    def request(self,client,filters,page=1,limit=PAGE_LIMIT,facets=()):
        api_filters,year=request_criteria(filters)
        config=dict(self.config,filters=self.config['filters']+api_filters,facets=list(facets),limit=limit)
        if year is not None:
            config.update(date_ranges=['publishDate'],publishDatefrom=year,publishDateto=year)
        url=build_url(config,page,limit=limit)
        saved=self.db.execute('SELECT sha,payload FROM api_cache WHERE url=?',(url,)).fetchone()
        if not saved:
            saved=self.recover_logged_raw(url)
        if saved:
            data=bytes(saved[1]); sha=saved[0]
            try: obj=validate(data,allow_record_rejections=True)
            except ValueError as exc:
                self.record_invalid_response(url,sha,data,exc)
                with self.db: self.db.execute('DELETE FROM api_cache WHERE url=?',(url,))
                raise
            self.log_api_cache(url,sha,len(data))
            self.last_api_source='cache'
            return obj,sha,url
        data=client.get(url); sha=self.archive(data)
        try: obj=validate(data,allow_record_rejections=True)
        except ValueError as exc:
            self.record_invalid_response(url,sha,data,exc)
            raise
        with self.db:
            self.db.execute('INSERT INTO api_cache VALUES(?,?,?,?)',(url,sha,data,now()))
        self.last_api_source='network'
        return obj,sha,url

    def record_invalid_response(self,url,sha,data,exc):
        try: obj=json.loads(data)
        except (ValueError,TypeError): obj=None
        status=obj.get('status') if isinstance(obj,dict) else None
        result=obj.get('resultCount') if isinstance(obj,dict) else None
        present=isinstance(obj,dict) and 'records' in obj
        records=obj.get('records') if present else None
        category=('resposta_erro_api' if isinstance(obj,dict) and status!='OK'
                  else 'estrutura_inesperada')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO api_invalid_responses VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (url,sha,now(),category,str(exc),json.dumps(status,ensure_ascii=False),
                             type(result).__name__,json.dumps(result,ensure_ascii=False),int(present),
                             type(records).__name__))

    def recover_logged_raw(self,url):
        """Religa um Raw completo ao URL quando a validação caiu antes do checkpoint."""
        log=self.directory/'logs/api_http.jsonl'
        if not log.exists(): return None
        events=[]
        for line in log.read_text(encoding='utf-8-sig').splitlines():
            try: event=json.loads(line)
            except (ValueError,TypeError): continue
            if event.get('source')=='network' and event.get('status')==200 and event.get('url')==url:
                events.append(event)
        raw_dir=self.directory/'raw/api'
        for event in reversed(events):
            candidates=[]
            if event.get('sha256'):
                candidates=[raw_dir/(event['sha256']+'.json')]
            elif event.get('bytes') and event.get('ts'):
                try: observed=datetime.fromisoformat(event['ts']).timestamp()
                except (ValueError,TypeError): continue
                candidates=[path for path in raw_dir.glob('*.json') if path.stat().st_size==event['bytes']
                            and abs(path.stat().st_mtime-observed)<=2]
            for path in candidates:
                if not path.exists(): continue
                data=path.read_bytes(); sha=digest(data)
                try: validate(data,allow_record_rejections=True)
                except ValueError as exc:
                    self.record_invalid_response(url,sha,data,exc)
                    continue
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO api_cache VALUES(?,?,?,?)',
                                    (url,sha,data,now()))
                return sha,data
        return None

    def select_archived_institution(self):
        """Escolhe literalmente uma instituição arquivada; a contagem é apenas global."""
        found={str(value):int(count) for value,count in self.db.execute(
            "SELECT value,count FROM facet_values WHERE field='institution'")}
        for payload, in self.db.execute('SELECT payload FROM api_cache'):
            try: items=json.loads(bytes(payload)).get('facets',{}).get('institution',[])
            except (ValueError,TypeError,AttributeError): continue
            for item in items:
                if isinstance(item,dict) and item.get('value') is not None:
                    try: found.setdefault(str(item['value']),int(item['count']))
                    except (KeyError,TypeError,ValueError): continue
        if not found:
            raise ValueError('Nenhuma instituição foi encontrada nas facetas arquivadas.')
        value,count=min(found.items(),key=lambda item:(item[1],item[0]))
        return dict(value=value,global_facet_count=count)

    @staticmethod
    def institution_fields(record):
        return {key:value for key,value in record.items()
                if 'institution' in str(key).casefold()}

    def log_api_cache(self,url,sha,size):
        path=self.directory/'logs/api_http.jsonl'
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(dict(ts=now(),url=url,status='cache_hit',attempt=0,
                                         bytes=size,error=None,source='cache',sha256=sha),
                                    ensure_ascii=False)+'\n')

    def import_compatibility(self,row):
        """Confirma pelo URL de origem que o registro veio da consulta atual ou de seu subconjunto."""
        if not set(row.get('formats',[])) & {'masterThesis','doctoralThesis'}:
            return None
        try:
            params=urllib.parse.parse_qs(urllib.parse.urlsplit(row['source_url']).query)
        except (KeyError,TypeError,ValueError):
            return None
        if not set(self.config['filters']) <= set(params.get('filter[]',[])):
            return None
        expected={(clause['lookfor'].casefold(),clause['type'])
                  for group in self.config.get('groups',[]) for clause in group.get('clauses',[])}
        observed={(term.casefold(),kind) for key,values in params.items() if key.startswith('lookfor')
                  for term,kind in zip(values,params.get(key.replace('lookfor','type'),[]))}
        if expected and expected <= observed:
            return 'consulta_atual_com_quatro_campos'
        lookfor=' '.join(params.get('lookfor',[])).casefold()
        if params.get('type')==['Subject'] and 'linguagem' in lookfor and 'comunicação' in lookfor:
            return 'subconjunto_logico_da_consulta_atual_por_assunto'
        return None

    def import_metadata(self,paths):
        stats=dict(files=0,rows_read=0,compatible_rows=0,unique_records_before=0,
                   unique_records_after=0,inserted_records=0,duplicate_records=0,
                   rejected_rows=0,sources=[])
        stats['unique_records_before']=self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        for source in map(Path,paths):
            data=source.read_bytes(); rows=load_rows(data); source_sha=digest(data)
            source_name=source.as_posix(); accepted=0; rejected=0; inserted=0
            for row in rows:
                basis=self.import_compatibility(row)
                if not basis:
                    rejected+=1; continue
                payload=json.dumps(row,ensure_ascii=False)
                exists=self.db.execute('SELECT payload FROM records WHERE record_id=?',
                                       (row['record_id'],)).fetchone()
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO record_sources VALUES(?,?,?,?,?,?,?)',
                                    (row['record_id'],source_name,row.get('query_id'),basis,
                                     source_sha,payload,now()))
                    if not exists:
                        self.db.execute('INSERT INTO records VALUES(?,?,?)',
                                        (row['record_id'],payload,now()))
                        inserted+=1
                accepted+=1
            stats['files']+=1; stats['rows_read']+=len(rows)
            stats['compatible_rows']+=accepted; stats['rejected_rows']+=rejected
            stats['inserted_records']+=inserted
            stats['duplicate_records']+=accepted-inserted
            stats['sources'].append(dict(path=source_name,sha256=source_sha,rows=len(rows),
                                         compatible=accepted,rejected=rejected,inserted=inserted))
        stats['unique_records_after']=self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        stats['generated_at']=now()
        atomic(self.directory/'reports/import_existing_metadata.json',
               json.dumps(stats,ensure_ascii=False,indent=2))
        self.export_metadata()
        return stats

    def reuse_downloads(self,worker,source_directories):
        worker.db.execute('''CREATE TABLE IF NOT EXISTS reuse_sources
          (source_dir TEXT PRIMARY KEY,manifest_sha TEXT,imported_at TEXT,records INTEGER,files INTEGER)''')
        known={row[0] for row in self.db.execute('SELECT record_id FROM records')}
        totals=dict(sources=0,records_reused=0,file_associations=0,unique_files=set(),
                    candidate_files=set(),site_document_files=set(),skipped_unknown_records=0)
        for source_dir in map(Path,source_directories):
            manifest=source_dir/'raw/manifest_pdf.jsonl'
            data=manifest.read_bytes(); manifest_sha=digest(data)
            source_name=source_dir.as_posix()
            if worker.db.execute('SELECT 1 FROM reuse_sources WHERE source_dir=? AND manifest_sha=?',
                                 (source_name,manifest_sha)).fetchone():
                continue
            rows=[json.loads(line) for line in data.decode('utf-8-sig').splitlines() if line.strip()]
            reused_records=0; reused_files=0
            for row in rows:
                rid=row.get('record_id')
                if rid not in known:
                    totals['skipped_unknown_records']+=1; continue
                files=[]
                for item in row.get('files',[]):
                    relative=Path(item['path'])
                    source_file=(source_dir/relative).resolve()
                    if source_dir.resolve() not in source_file.parents:
                        raise ValueError('Caminho de PDF importado escapa da origem: '+str(relative))
                    if not source_file.exists() or file_hash(source_file)!=item['sha256']:
                        raise ValueError('PDF importado ausente ou com SHA-256 divergente: '+str(source_file))
                    target_relative=Path('raw/pdf')/item['sha256'][:2]/(item['sha256']+'.pdf')
                    target=worker.directory/target_relative; target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists() and file_hash(target)!=item['sha256']:
                        raise ValueError('Colisão no destino ao reaproveitar PDF: '+str(target))
                    if not target.exists():
                        try: os.link(source_file,target)
                        except OSError: shutil.copy2(source_file,target)
                    copied=dict(item,path=target_relative.as_posix(),kind='pdf',cached=True,
                                reused_from=source_name)
                    files.append(copied); reused_files+=1; totals['unique_files'].add(item['sha256'])
                    bucket=('site_document_files' if obvious_site_document(item.get('url',''))
                            else 'candidate_files')
                    totals[bucket].add(item['sha256'])
                    if item.get('url'):
                        with worker.db:
                            worker.db.execute('INSERT OR IGNORE INTO cache VALUES(?,?)',
                                              (item['url'],json.dumps(copied,ensure_ascii=False)))
                existing=worker.db.execute('SELECT 1 FROM results WHERE id=?',(rid,)).fetchone()
                if not existing:
                    imported=dict(row,files=files,input_sha256=worker.input_sha,
                                  reused_from=source_name,reused_manifest_sha256=manifest_sha)
                    imported['limited']=bool(row.get('limited') or row.get('status')!='sucesso' or
                        any(a.get('status') not in COMPLETED_ATTEMPTS for a in row.get('attempts',[])))
                    with worker.db:
                        worker.db.execute('INSERT INTO results VALUES(?,?)',
                                          (rid,json.dumps(imported,ensure_ascii=False)))
                    reused_records+=1
            with worker.db:
                worker.db.execute('INSERT OR REPLACE INTO reuse_sources VALUES(?,?,?,?,?)',
                                  (source_name,manifest_sha,now(),reused_records,reused_files))
            totals['sources']+=1; totals['records_reused']+=reused_records
            totals['file_associations']+=reused_files
        source_stats=worker.db.execute('''SELECT COUNT(*),COALESCE(SUM(records),0),
                                          COALESCE(SUM(files),0) FROM reuse_sources''').fetchone()
        imported_files={f['sha256']:f for saved in worker.db.execute('SELECT payload FROM results')
                        for f in json.loads(saved[0]).get('files',[]) if f.get('reused_from')}
        totals['sources']=source_stats[0]
        totals['records_reused']=source_stats[1]
        totals['file_associations']=source_stats[2]
        totals['unique_files']=len(imported_files)
        totals['candidate_files']=sum(not obvious_site_document(item.get('url',''))
                                      for item in imported_files.values())
        totals['site_document_files']=sum(obvious_site_document(item.get('url',''))
                                          for item in imported_files.values())
        totals['generated_at']=now()
        atomic(self.directory/'reports/reused_downloads.json',
               json.dumps(totals,ensure_ascii=False,indent=2))
        return totals

    def bootstrap_cooldown_from_log(self,fallback_seconds):
        """Migra o último 429 legado para impedir que uma reinicialização antecipe o prazo."""
        origin='{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(self.config['endpoint']))
        endpoint_path=urllib.parse.urlsplit(self.config['endpoint']).path
        events=[]
        for path in (self.directory/'logs/api_http.jsonl',
                     self.directory/'logs/api_diagnostic_http.jsonl'):
            if not path.exists(): continue
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                try: event=json.loads(line); observed=datetime.fromisoformat(event['ts'])
                except (ValueError,TypeError,KeyError): continue
                event_origin='{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(event.get('url','')))
                if (event.get('source','network')=='network' and event_origin==origin and
                        urllib.parse.urlsplit(event.get('url','')).path==endpoint_path):
                    events.append((observed,event))
        latest_429=max((item for item in events if item[1].get('status')==429),default=None,key=lambda item:item[0])
        latest_success=max((item for item in events if isinstance(item[1].get('status'),int)
                            and 200<=item[1]['status']<300),default=None,key=lambda item:item[0])
        if latest_success and (not latest_429 or latest_success[0]>latest_429[0]):
            self.clear_cooldown(self.config['endpoint'],latest_success[0])
            return
        if self.active_cooldown(self.config['endpoint']) or not latest_429: return
        latest=latest_429[1]
        observed=datetime.fromisoformat(latest['ts'])
        blocked_until=observed+timedelta(seconds=fallback_seconds)
        if blocked_until<=datetime.now(timezone.utc): return
        evidence=dict(latest,legacy_log=True,headers_not_recorded=True,
                      fallback_policy_seconds=fallback_seconds)
        self.record_rate_limit(latest['url'],None,blocked_until,
                               'client_fallback_from_legacy_log',evidence,
                               observed_at=observed.isoformat())

    def record_rate_limit(self,url,retry_after,blocked_until,wait_source,evidence,observed_at=None):
        origin='{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(url))
        observed_at=observed_at or now()
        with self.db:
            cursor=self.db.execute('''INSERT INTO api_rate_limits
              (observed_at,url,status,retry_after,blocked_until,wait_source,evidence)
              VALUES(?,?,?,?,?,?,?)''',(observed_at,url,429,retry_after,
                                       blocked_until.astimezone(timezone.utc).isoformat(),wait_source,
                                       json.dumps(evidence,ensure_ascii=False)))
            self.db.execute('INSERT OR REPLACE INTO api_cooldowns VALUES(?,?,?,?,?,?)',
                            (origin,blocked_until.astimezone(timezone.utc).isoformat(),
                             observed_at,url,wait_source,cursor.lastrowid))

    def active_cooldown(self,url):
        origin='{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(url))
        row=self.db.execute('SELECT blocked_until,wait_source,url FROM api_cooldowns WHERE origin=?',
                            (origin,)).fetchone()
        if not row: return None
        deadline=datetime.fromisoformat(row[0])
        if deadline<=datetime.now(timezone.utc): return None
        return dict(origin=origin,blocked_until=deadline,wait_source=row[1],url=row[2])

    def clear_cooldown(self,url,success_at=None):
        origin='{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(url))
        row=self.db.execute('SELECT observed_at FROM api_cooldowns WHERE origin=?',(origin,)).fetchone()
        if not row: return False
        if success_at is not None and datetime.fromisoformat(row[0])>success_at:
            return False
        with self.db: self.db.execute('DELETE FROM api_cooldowns WHERE origin=? AND observed_at=?',
                                     (origin,row[0]))
        return True

    def validation_step(self,name,payload):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO validations VALUES(?,?,?)',
                            (name,json.dumps(payload,ensure_ascii=False),now()))
        rows={row[0]:json.loads(row[1]) for row in self.db.execute(
            "SELECT name,payload FROM validations WHERE name LIKE 'step_%' ORDER BY name")}
        atomic(self.directory/'reports/partition_validation_progress.json',
               json.dumps(dict(version=VERSION,updated_at=now(),steps=rows),
                          ensure_ascii=False,indent=2))

    def validate_partitioning(self,client):
        root,root_sha,root_url=self.request(client,[],limit=1,facets=('publishDate','institution'))
        years=root.get('facets',{}).get('publishDate',[])
        if not years: raise ValueError('A API não retornou a faceta publishDate.')
        institutions=root.get('facets',{}).get('institution',[])
        if not institutions: raise ValueError('A API não retornou a faceta institution.')
        with self.db:
            self.db.execute('UPDATE settings SET root_total=? WHERE id=1',(root['resultCount'],))
        self.validation_step('step_root',dict(observed=root['resultCount'],sha256=root_sha,
                                              url=root_url,years=len(years),institutions=len(institutions)))
        # Usa a menor partição exposta para validar a sintaxe com custo reduzido.
        year_item=min((item for item in years if int(item['count'])>0),key=lambda item:int(item['count']))
        year=str(year_item['value']); year_expected=year_item['count']
        year_filter=year_criterion(year)
        by_year,year_sha,year_url=self.request(client,[year_filter],limit=1)
        if by_year['resultCount']!=year_expected:
            raise ValueError('Filtro de ano divergiu da contagem da faceta.')
        self.validation_step('step_year',dict(filter=year_filter,expected=year_expected,
                                              observed=by_year['resultCount'],sha256=year_sha,url=year_url))
        institution=str(institutions[0]['value'])
        inst_filter=quoted_filter('institution',institution)
        child,child_sha,child_url=self.request(client,[year_filter,inst_filter],limit=1)
        self.validation_step('step_institution',dict(filters=[year_filter,inst_filter],
            observed=child['resultCount'],sha256=child_sha,url=child_url))
        negative=quoted_filter('institution',institution,negative=True)
        residual,residual_sha,residual_url=self.request(client,[year_filter,negative],limit=1)
        expected_residual=by_year['resultCount']-child['resultCount']
        residual_matches=residual['resultCount']==expected_residual
        self.validation_step('step_institution_complement',dict(filters=[year_filter,negative],
            expected=expected_residual,observed=residual['resultCount'],matches=residual_matches,
            sha256=residual_sha,url=residual_url))
        no_year,no_year_sha,no_year_url=self.request(client,['-publishDate:[* TO *]'],limit=1)
        self.validation_step('step_no_year',dict(filter='-publishDate:[* TO *]',
            observed=no_year['resultCount'],sha256=no_year_sha,url=no_year_url))
        other_year_filters=['publishDate:[* TO *]',*(quoted_filter('publishDate',item['value'],True)
                                                     for item in years)]
        other_years,other_years_sha,other_years_url=self.request(client,other_year_filters,limit=1)
        other_years_expected=root['resultCount']-sum(int(item['count']) for item in years)-no_year['resultCount']
        self.validation_step('step_other_year_values',dict(filters=other_year_filters,
            expected=other_years_expected,observed=other_years['resultCount'],
            matches=other_years['resultCount']==other_years_expected,
            sha256=other_years_sha,url=other_years_url))
        boundary={}
        if child['resultCount']:
            last_page=min(10,math.ceil(child['resultCount']/PAGE_LIMIT))
            last,last_sha,last_url=self.request(client,[year_filter,inst_filter],page=last_page)
            after,after_sha,after_url=self.request(client,[year_filter,inst_filter],page=last_page+1)
            repeated=bool(last['records'] and after['records'] and
                          fingerprint(last['records'])==fingerprint(after['records']))
            boundary=dict(last_page=last_page,last_count=len(last['records']),
                          after_count=len(after['records']),after_repeats_last=repeated,
                          expected_empty=child['resultCount']<=WINDOW,
                          valid=(not after['records']) if child['resultCount']<=WINDOW else repeated,
                          evidence=[last_sha,after_sha],urls=[last_url,after_url])
            self.validation_step('step_pagination_boundary',boundary)
        result=dict(version=VERSION,validated_at=now(),root_total=root['resultCount'],
                    year=dict(value=year,expected=year_expected,observed=by_year['resultCount']),
                    institution=dict(value=institution,observed=child['resultCount']),
                    residual_institution_complement=dict(filter=negative,expected=expected_residual,
                                                         observed=residual['resultCount'],matches=residual_matches),
                    no_year=dict(filter='-publishDate:[* TO *]',total=no_year['resultCount']),
                    other_year_values=dict(filters=other_year_filters,expected=other_years_expected,
                                           observed=other_years['resultCount'],
                                           matches=other_years['resultCount']==other_years_expected),
                    boundary=boundary,
                    evidence=[dict(name=name,sha256=sha,url=url) for name,sha,url in (
                        ('root',root_sha,root_url),('year',year_sha,year_url),
                        ('institution',child_sha,child_url),('residual',residual_sha,residual_url),
                        ('no_year',no_year_sha,no_year_url),
                        ('other_year_values',other_years_sha,other_years_url))])
        if not residual_matches or not boundary.get('valid',False):
            raise ValueError('Validação real do particionamento apresentou divergência.')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO validations VALUES(?,?,?)',
                            ('partition_filters',json.dumps(result,ensure_ascii=False),now()))
        atomic(self.directory/'reports/partition_validation.json',
               json.dumps(result,ensure_ascii=False,indent=2))
        return result

    def add_partition(self,label,filters,parent,priority,expected=None):
        part_id=digest(json.dumps(filters,sort_keys=True,ensure_ascii=False).encode())[:24]
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO partitions
              VALUES(?,?,?,?,?,'planned',NULL,1,?,0,NULL,?,?)''',
              (part_id,label,json.dumps(filters,ensure_ascii=False),parent,priority,expected,now(),now()))
        return part_id

    def plan_root(self,client):
        if self.db.execute('SELECT 1 FROM partitions LIMIT 1').fetchone(): return
        root,sha,url=self.request(client,[],limit=1,facets=('publishDate','institution'))
        years=root.get('facets',{}).get('publishDate',[])
        institutions=root.get('facets',{}).get('institution',[])
        year_items=sorted(years,key=lambda item:str(item['value']),reverse=True)
        if not year_items: raise ValueError('Não foi possível descobrir anos para particionar.')
        if not institutions: raise ValueError('Não foi possível descobrir instituições para subdividir.')
        with self.db:
            for field,items in (('publishDate',years),('institution',institutions)):
                for item in items:
                    self.db.execute('INSERT OR REPLACE INTO facet_values VALUES(?,?,?,?)',
                                    (field,str(item['value']),int(item['count']),now()))
        for index,item in enumerate(year_items):
            year=str(item['value'])
            self.add_partition(f'ano-{year}',[year_criterion(year)],None,index,item['count'])
        priority=len(year_items)
        residual_years=['publishDate:[* TO *]',*(quoted_filter('publishDate',item['value'],True)
                                                 for item in year_items)]
        self.add_partition('outros-valores-de-ano',residual_years,None,priority)
        self.add_partition('sem-ano',['-publishDate:[* TO *]'],None,priority+1)
        with self.db:
            self.db.execute('UPDATE settings SET root_total=? WHERE id=1',(root['resultCount'],))
        atomic(self.directory/'reports/root_discovery.json',json.dumps(dict(
            version=VERSION,discovered_at=now(),root_total=root['resultCount'],
            facet_years=[str(item['value']) for item in year_items],
            facet_institutions=[str(item['value']) for item in institutions],
            raw_sha256=sha,url=url,
            residual_strategy=['campo de ano presente menos valores expostos pela faceta',
                               'sem ano por filtro negativo']),
            ensure_ascii=False,indent=2))

    def archived_years(self):
        found={str(value) for value, in self.db.execute(
            "SELECT value FROM facet_values WHERE field='publishDate'")}
        for payload, in self.db.execute('SELECT payload FROM api_cache'):
            try: items=json.loads(bytes(payload)).get('facets',{}).get('publishDate',[])
            except (ValueError,TypeError,AttributeError): continue
            found.update(str(item['value']) for item in items
                         if isinstance(item,dict) and item.get('value') is not None)
        found.add('2024')
        return ['2024']+sorted((value for value in found if value!='2024'),reverse=True)

    def plan_annual_windows(self):
        for priority,year in enumerate(self.archived_years()):
            label=ANNUAL_WINDOW_PREFIX+year
            part_id=digest(('annual-window-v1\n'+year).encode())[:24]
            with self.db:
                self.db.execute('''INSERT OR IGNORE INTO partitions
                  VALUES(?,?,?,?,?,'planned',NULL,1,NULL,0,NULL,?,?)''',
                  (part_id,label,json.dumps([year_criterion(year)]),None,priority,now(),now()))

    def import_archived_annual_pages(self,after_page=None):
        """Importa somente diagnósticos cuja URL coincide exatamente com a consulta anual."""
        reports=list((self.directory/'reports').glob('api_diagnostic_2024_page_*.json'))
        legacy=self.directory/'reports/api_diagnostic_2024.json'
        if legacy.exists(): reports.append(legacy)
        part=self.db.execute('SELECT id FROM partitions WHERE label=?',
                             (ANNUAL_WINDOW_PREFIX+'2024',)).fetchone()
        if not part: return 0
        imported=0
        for report_path in sorted(set(reports),key=lambda path:path.name):
            try:
                report=json.loads(report_path.read_text(encoding='utf-8'))
                page=int(report.get('page') or urllib.parse.parse_qs(
                    urllib.parse.urlsplit(report['request_url']).query)['page'][0])
                expected_config=dict(self.config,filters=self.config['filters'],facets=[],
                                     date_ranges=['publishDate'],publishDatefrom='2024',
                                     publishDateto='2024',limit=ANNUAL_PAGE_LIMIT)
                expected_url=build_url(expected_config,page,limit=ANNUAL_PAGE_LIMIT)
                if report.get('status')!=200 or report.get('request_url')!=expected_url: continue
                response=(self.directory/report['response']['path']).resolve()
                if self.directory.resolve() not in response.parents: continue
                data=response.read_bytes()
                if digest(data)!=report['response']['sha256']: continue
                obj=validate(data,allow_record_rejections=True); sha=self.archive(data)
            except (OSError,ValueError,KeyError,TypeError):
                continue
            if self.db.execute('SELECT 1 FROM pages WHERE partition_id=? AND page=?',(part[0],page)).fetchone():
                continue
            self.store_records(part[0],page,obj,sha,expected_url)
            obtained_pages={value[0] for value in self.db.execute(
                'SELECT page FROM pages WHERE partition_id=?',(part[0],))}
            next_page=next((candidate for candidate in range(1,ANNUAL_MAX_PAGES+1)
                            if candidate not in obtained_pages),ANNUAL_MAX_PAGES+1)
            unique_ids=self.db.execute('SELECT COUNT(*) FROM record_partitions WHERE partition_id=?',
                                       (part[0],)).fetchone()[0]
            if unique_ids>=obj['resultCount']:
                status,reason,inaccessible='complete',None,0
            elif next_page>ANNUAL_MAX_PAGES:
                status,reason,inaccessible='partial','limite_de_1000_posicoes',max(0,obj['resultCount']-unique_ids)
            else:
                status,reason,inaccessible='harvesting',None,0
            with self.db:
                self.db.execute('''UPDATE partitions SET status=?,total=?,next_page=?,
                  inaccessible_records=?,error=?,updated_at=? WHERE id=?''',
                  (status,obj['resultCount'],next_page,inaccessible,reason,now(),part[0]))
            imported+=1
            self.export_metadata(); self.annual_windows_report()
            if after_page: after_page()
        return imported

    def harvest_annual_window(self,client):
        row=self.db.execute("""SELECT * FROM partitions
          WHERE label LIKE ? AND status IN ('planned','harvesting') ORDER BY priority,id LIMIT 1""",
          (ANNUAL_WINDOW_PREFIX+'%',)).fetchone()
        if not row: return False
        part_id,label=row[0],row[1]; page=1 if row[5]=='planned' else row[7]
        filters=json.loads(row[2])
        persisted=self.db.execute('SELECT COALESCE(SUM(count),0) FROM pages WHERE partition_id=?',
                                  (part_id,)).fetchone()[0]
        persisted+=self.db.execute('SELECT COUNT(*) FROM metadata_rejections WHERE partition_id=?',
                                   (part_id,)).fetchone()[0]
        if row[6] is not None and persisted>=row[6]:
            with self.db:
                self.db.execute("UPDATE partitions SET status='complete',inaccessible_records=0,error=NULL,updated_at=? WHERE id=?",
                                (now(),part_id))
            self.annual_windows_report(); return True
        try:
            obj,sha,url=self.request(client,filters,page=page,limit=ANNUAL_PAGE_LIMIT)
            if row[6] is not None and obj['resultCount']!=row[6]:
                raise ValueError('Total mudou durante a janela anual.')
            if not obj['records'] and persisted<obj['resultCount']:
                raise ValueError('Página válida vazia antes de persistir o total informado.')
            self.store_records(part_id,page,obj,sha,url)
            total=obj['resultCount']; pages=page
            obtained=self.db.execute('SELECT COUNT(*) FROM record_partitions WHERE partition_id=?',
                                     (part_id,)).fetchone()[0]
            rejected=self.db.execute('SELECT COUNT(*) FROM metadata_rejections WHERE partition_id=?',
                                     (part_id,)).fetchone()[0]
            positions=self.db.execute('SELECT COALESCE(SUM(count),0) FROM pages WHERE partition_id=?',
                                      (part_id,)).fetchone()[0]+rejected
            if positions>=total:
                status,reason,inaccessible='complete',None,0
            elif pages>=ANNUAL_MAX_PAGES:
                status,reason,inaccessible='partial','limite_de_1000_posicoes',max(0,total-obtained)
            else:
                status,reason,inaccessible='harvesting',None,0
            with self.db:
                self.db.execute('''UPDATE partitions SET status=?,total=?,next_page=?,
                  inaccessible_records=?,error=?,updated_at=? WHERE id=?''',
                  (status,total,page+1,inaccessible,reason,now(),part_id))
        except ValueError as exc:
            if not str(exc).startswith('Página repetida:'): raise
            total=row[6] or 0
            obtained=self.db.execute('SELECT COUNT(*) FROM record_partitions WHERE partition_id=?',
                                     (part_id,)).fetchone()[0]
            with self.db:
                self.db.execute("""UPDATE partitions SET status='partial',inaccessible_records=?,
                  error='pagina_repetida',updated_at=? WHERE id=?""",
                  (max(0,total-obtained),now(),part_id))
        self.export_metadata(); self.annual_windows_report()
        return True

    def annual_windows_report(self,stop_reason=None):
        years=[]
        for part_id,label,status,total,error in self.db.execute('''SELECT id,label,status,total,error
          FROM partitions WHERE label LIKE ? ORDER BY priority,id''',(ANNUAL_WINDOW_PREFIX+'%',)):
            pages=self.db.execute('SELECT COUNT(*) FROM pages WHERE partition_id=?',(part_id,)).fetchone()[0]
            unique_ids=self.db.execute('SELECT COUNT(*) FROM record_partitions WHERE partition_id=?',
                                       (part_id,)).fetchone()[0]
            rejected=self.db.execute('SELECT COUNT(*) FROM metadata_rejections WHERE partition_id=?',
                                     (part_id,)).fetchone()[0]
            positions=self.db.execute('SELECT COALESCE(SUM(count),0) FROM pages WHERE partition_id=?',
                                      (part_id,)).fetchone()[0]+rejected
            years.append(dict(year=label[len(ANNUAL_WINDOW_PREFIX):],total_reported=total,
                              pages_obtained=pages,unique_ids=unique_ids,rejected_records=rejected,
                              positions_persisted=positions,status=status,
                              interruption_reason=error,
                              partial_coverage=(positions<total if total is not None else None)))
        report=dict(version=VERSION,updated_at=now(),mode='annual_windows',page_limit=ANNUAL_PAGE_LIMIT,
                    max_pages_per_year=ANNUAL_MAX_PAGES,stop_reason=stop_reason,years=years)
        atomic(self.directory/'reports/annual_windows.json',json.dumps(report,ensure_ascii=False,indent=2))
        return report

    def store_records(self,partition_id,page,obj,sha,url):
        existing=self.db.execute('SELECT sha,count,url FROM pages WHERE partition_id=? AND page=?',
                                 (partition_id,page)).fetchone()
        if existing:
            if existing[0]!=sha or existing[2]!=url:
                raise ValueError('Checkpoint da página diverge da resposta retomada.')
            return existing[1]
        ids=[row['id'] for row in obj['records']]
        rejections=obj.get('_record_rejections',[])
        fp=digest(json.dumps(dict(ids=ids,rejections=[(item['position'],item['reason'],item['payload'])
                                                      for item in rejections]),
                             sort_keys=True,ensure_ascii=False).encode())
        repeated=self.db.execute('SELECT page FROM pages WHERE partition_id=? AND fingerprint=?',
                                 (partition_id,fp)).fetchone() if ids else None
        if repeated:
            raise ValueError(f'Página repetida: página {page} repete {repeated[0]}.')
        filters=json.loads(self.db.execute('SELECT filters FROM partitions WHERE id=?',(partition_id,)).fetchone()[0])
        ts=now()
        with self.db:
            for item in rejections:
                payload=item.get('payload')
                record_id=payload.get('id') if isinstance(payload,dict) else None
                self.db.execute('INSERT OR REPLACE INTO metadata_rejections VALUES(?,?,?,?,?,?,?,?)',
                                (partition_id,page,item['position'],record_id,item['reason'],
                                 json.dumps(payload,ensure_ascii=False),sha,ts))
            for row in obj['records']:
                provenance=dict(source_url=url,harvest_ts=ts,sha256=sha,pipeline_version=VERSION,
                                query_id=self.signature,page=page,partition_id=partition_id,
                                partition_filters=filters)
                value=normalize(row,provenance)
                prior=self.db.execute('SELECT payload FROM records WHERE record_id=?',(row['id'],)).fetchone()
                if not prior:
                    self.db.execute('INSERT INTO records VALUES(?,?,?)',
                                    (row['id'],json.dumps(value,ensure_ascii=False),ts))
                elif json.loads(prior[0]).get('original_record')!=row:
                    self.db.execute('INSERT OR IGNORE INTO conflicts VALUES(?,?,?,?,?)',
                                    (row['id'],partition_id,page,json.dumps(row,ensure_ascii=False),ts))
                self.db.execute('INSERT OR IGNORE INTO record_partitions VALUES(?,?,?)',
                                (row['id'],partition_id,page))
            self.db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?,?)',
                            (partition_id,page,obj['resultCount'],len(ids),sha,fp,url,ts))
        return len(ids)

    def prepare(self,client,row):
        part_id,label,filters_json=row[0],row[1],row[2]
        filters=json.loads(filters_json)
        obj,sha,url=self.request(client,filters,page=1)
        total=obj['resultCount']; expected=row[8]
        if expected is not None and expected!=total:
            raise ValueError(f'Contagem da partição {label} divergiu da faceta: {expected} != {total}.')
        if total>WINDOW:
            # Partições filhas que ainda excedem a janela são registradas como
            # parciais após a prova da página repetida; repetir o mesmo filtro de
            # instituição não as subdividiria.
            if row[3] is not None:
                self.store_records(part_id,1,obj,sha,url)
                with self.db:
                    self.db.execute("UPDATE partitions SET status='harvesting',total=?,next_page=2,error=?,updated_at=? WHERE id=?",
                                    (total,'subpartição excede 1.000; a janela acessível será coletada e registrada',now(),part_id))
                return
            institutions=[dict(value=value,count=count) for value,count in self.db.execute(
                "SELECT value,count FROM facet_values WHERE field='institution' ORDER BY count DESC,value")]
            if not institutions:
                self.store_records(part_id,1,obj,sha,url)
                with self.db:
                    self.db.execute("UPDATE partitions SET status='harvesting',total=?,next_page=2,error=?,updated_at=? WHERE id=?",
                                    (total,'sem faceta de instituição; somente a janela será acessível',now(),part_id))
                return
            base_priority=row[4]*1000
            for index,item in enumerate(institutions):
                self.add_partition(label+'--instituicao-'+str(item['value']),
                                   filters+[quoted_filter('institution',item['value'])],
                                   part_id,base_priority+index)
            residual_filters=filters+[quoted_filter('institution',item['value'],negative=True)
                                      for item in institutions]
            self.add_partition(label+'--outras-ou-sem-instituicao',residual_filters,
                               part_id,base_priority+len(institutions))
            with self.db:
                self.db.execute("UPDATE partitions SET status='split',total=?,updated_at=? WHERE id=?",
                                (total,now(),part_id))
            return
        count=self.store_records(part_id,1,obj,sha,url)
        status='complete' if count>=total else 'harvesting'
        with self.db:
            self.db.execute('UPDATE partitions SET status=?,total=?,next_page=2,updated_at=? WHERE id=?',
                            (status,total,now(),part_id))

    def harvest_one(self,client):
        row=self.db.execute("""SELECT * FROM partitions WHERE status IN ('planned','harvesting')
                               ORDER BY priority,id LIMIT 1""").fetchone()
        if not row: return False
        part_id,label,status=row[0],row[1],row[5]
        try:
            if status=='planned':
                self.prepare(client,row)
                self.export_metadata(); self.report()
                return True
            total=row[6]; page=row[7]; filters=json.loads(row[2])
            obj,sha,url=self.request(client,filters,page=page)
            if obj['resultCount']!=total: raise ValueError('Total mudou durante a partição.')
            count=self.store_records(part_id,page,obj,sha,url)
            accessible=min(total,WINDOW); expected_pages=math.ceil(accessible/PAGE_LIMIT) if accessible else 0
            if page>expected_pages:
                if count and page>10:
                    with self.db:
                        self.db.execute("UPDATE partitions SET status='partial',inaccessible_records=?,error=?,updated_at=? WHERE id=?",
                                        (max(0,total-WINDOW),'API repetiu ou retornou dados após a janela',now(),part_id))
                elif count:
                    raise ValueError('Dados inesperados depois do fim calculado.')
                else:
                    with self.db:
                        self.db.execute("UPDATE partitions SET status='complete',updated_at=? WHERE id=?",(now(),part_id))
                return True
            next_page=page+1
            if page==expected_pages:
                if total>WINDOW:
                    # A próxima iteração prova e registra a repetição da página 10.
                    next_page=11
                else:
                    with self.db:
                        self.db.execute("UPDATE partitions SET status='complete',next_page=?,updated_at=? WHERE id=?",
                                        (next_page,now(),part_id))
                    self.export_metadata(); self.report(); return True
            with self.db:
                self.db.execute('UPDATE partitions SET next_page=?,updated_at=? WHERE id=?',
                                (next_page,now(),part_id))
        except Exception as exc:
            if (isinstance(exc,urllib.error.HTTPError) and exc.code in (429,500,502,503,504)) or \
               (isinstance(exc,RuntimeError) and str(exc).startswith('Servidor pediu espera')):
                # O estado continua planned/harvesting. Uma execução posterior
                # retoma a mesma página em vez de declarar perda de cobertura.
                raise
            known_total=row[6] or row[8] or 0
            repeated_page=str(exc).startswith('Página repetida:')
            final_status='partial' if repeated_page and known_total>WINDOW else 'inaccessible'
            inaccessible=max(0,known_total-WINDOW) if final_status=='partial' else known_total
            with self.db:
                self.db.execute('UPDATE partitions SET status=?,error=?,inaccessible_records=?,updated_at=? WHERE id=?',
                                (final_status,str(exc),inaccessible,now(),part_id))
        self.export_metadata(); self.report(); return True

    def export_metadata(self):
        rows=[json.loads(row[0]) for row in self.db.execute('SELECT payload FROM records ORDER BY rowid')]
        path=self.directory/'metadata/records.jsonl'
        atomic(path,''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
        rejected=[dict(partition_id=row[0],page=row[1],position=row[2],record_id=row[3],
                       reason=row[4],payload=json.loads(row[5]),raw_sha256=row[6],detected_at=row[7])
                  for row in self.db.execute('''SELECT partition_id,page,position,record_id,reason,
                    payload,raw_sha,detected_at FROM metadata_rejections ORDER BY partition_id,page,position''')]
        atomic(self.directory/'metadata/rejections.jsonl',''.join(
            json.dumps(row,ensure_ascii=False)+'\n' for row in rejected))
        return path,rows

    def download_available(self,contact,pdf_client,max_records=None,progress=None,deadline=None):
        path,rows=self.export_metadata(); data=path.read_bytes()
        if not rows: return 0,None
        worker=Downloader(self.directory/'pdfs',data,allow_growing_input=True)
        archived_hashes=set()
        batch_db_path=self.directory/'batches/batches.sqlite'
        if batch_db_path.exists():
            batch_db=sqlite3.connect(batch_db_path)
            try:
                columns={row[1] for row in batch_db.execute('PRAGMA table_info(batches)')}
                if 'archive_status' in columns:
                    archived_hashes={row[0] for row in batch_db.execute('''SELECT files.sha FROM files
                      JOIN batches ON batches.number=files.batch_number
                      WHERE batches.archive_status='transferred_verified' ''')}
            finally: batch_db.close()
        attempted=0
        try:
            if getattr(self,'reuse_pdf_dirs',None):
                self.reuse_downloads(worker,self.reuse_pdf_dirs)
            for row in rows:
                if deadline is not None and time.monotonic()>=deadline: break
                if row['record_id'] in getattr(self,'download_attempted_ids',set()): continue
                prior=worker.db.execute('SELECT payload FROM results WHERE id=?',(row['record_id'],)).fetchone()
                retry=False
                if prior:
                    old=json.loads(prior[0])
                    valid_files=all(item['sha256'] in archived_hashes or
                                    ((worker.directory/item['path']).exists() and
                                    file_hash(worker.directory/item['path'])==item['sha256'])
                                    for item in old.get('files',[]))
                    if old.get('status')=='sucesso' and valid_files and not old.get('limited'):
                        continue
                    try:
                        age=(datetime.now(timezone.utc)-datetime.fromisoformat(old['finished_at'])).total_seconds()
                    except (KeyError,TypeError,ValueError):
                        age=None
                    if age is not None and age<getattr(self,'repository_retry_seconds',24*3600):
                        continue
                    retry=True
                if max_records is not None and attempted>=max_records: break
                if pdf_client.transferred>=pdf_client.budget: break
                pdf_client.record_id=row['record_id']
                try: result=worker.process(row,pdf_client,retry=retry,candidate_limit=self.target,
                                           event_callback=progress.download_event if progress else None)
                finally: pdf_client.record_id=None
                self.download_attempted_ids.add(row['record_id'])
                attempted+=1
                report=worker.export(len(rows))
                failures=collections.Counter(a['status'] for item in report['results']
                    for a in item['attempts'] if a['status'] not in COMPLETED_ATTEMPTS)
                if progress:
                    progress.update_pdf(report,failures)
                else:
                    print('progresso:',f'tentados={report["processed_records"]}',
                          f'trabalhos_pdf={report["works_with_candidate_pdf"]}',
                          f'pdfs_unicos_candidatos={report["candidate_work_pdfs"]}',
                          f'bytes={report["pdf_bytes"]}',f'falhas={dict(failures)}',flush=True)
                if report['candidate_work_pdfs']>=self.target: break
                if any(a['status'] in ('limite_execucao','disco_reserva') for a in result['attempts']): break
            return attempted,worker.export(len(rows))
        finally: worker.db.close()

    def report(self,pdf_report=None,stop_reason=None):
        parts=[dict(label=row[0],filters=json.loads(row[1]),status=row[2],total=row[3],
                    next_page=row[4],inaccessible_records=row[5],error=row[6])
               for row in self.db.execute('''SELECT label,filters,status,total,next_page,
                                                     inaccessible_records,error
                                              FROM partitions ORDER BY priority,id''')]
        if pdf_report is None:
            path=self.directory/'pdfs/reports/pdf_pilot.json'
            pdf_report=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        failure_counts=pdf_report.get('failure_statuses')
        if failure_counts is None:
            failure_counts={key:value for key,value in pdf_report.get('attempt_statuses',{}).items()
                            if key not in COMPLETED_ATTEMPTS}
        root_total=self.db.execute('SELECT root_total FROM settings WHERE id=1').fetchone()[0]
        cooldown=self.active_cooldown(self.config['endpoint'])
        batch_summary=dict(closed_batches=0,candidate_pdf_files=0,bytes=0,
                           directory='batches')
        batch_db_path=self.directory/'batches/batches.sqlite'
        if batch_db_path.exists():
            batch_db=sqlite3.connect(batch_db_path)
            try:
                values=batch_db.execute("SELECT COUNT(*),COALESCE(SUM(files),0),COALESCE(SUM(bytes),0) FROM batches WHERE status='closed'").fetchone()
                batch_summary.update(closed_batches=values[0],candidate_pdf_files=values[1],bytes=values[2])
            finally:
                batch_db.close()
        split_coverage=[]
        for parent_id,label,total in self.db.execute("SELECT id,label,total FROM partitions WHERE status='split'"):
            children=self.db.execute('SELECT status,total FROM partitions WHERE parent_id=?',(parent_id,)).fetchall()
            known=[value for _,value in children if value is not None]
            all_known=bool(children) and len(known)==len(children)
            split_coverage.append(dict(label=label,parent_total=total,children=len(children),
                                       children_with_total=len(known),children_total=sum(known),
                                       all_totals_known=all_known,
                                       sum_matches_parent=(sum(known)==total) if all_known else None))
        report=dict(version=VERSION,updated_at=now(),query_id=self.signature,root_total=root_total,
                    target_candidate_unique_pdfs=self.target,
                    metadata_records=self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0],
                    record_partition_links=self.db.execute('SELECT COUNT(*) FROM record_partitions').fetchone()[0],
                    duplicate_partition_links=max(0,self.db.execute('SELECT COUNT(*) FROM record_partitions').fetchone()[0]-
                                                  self.db.execute('SELECT COUNT(DISTINCT record_id) FROM record_partitions').fetchone()[0]),
                    metadata_conflicts=self.db.execute('SELECT COUNT(*) FROM conflicts').fetchone()[0],
                    metadata_rejections=self.db.execute('SELECT COUNT(*) FROM metadata_rejections').fetchone()[0],
                    imported_metadata=dict(
                        records=self.db.execute('SELECT COUNT(DISTINCT record_id) FROM record_sources').fetchone()[0],
                        source_associations=self.db.execute('SELECT COUNT(*) FROM record_sources').fetchone()[0]),
                    api_http_state=dict(
                        successful_cache_entries=self.db.execute('SELECT COUNT(*) FROM api_cache').fetchone()[0],
                        rate_limit_events=self.db.execute('SELECT COUNT(*) FROM api_rate_limits').fetchone()[0],
                        active_cooldown=(dict(origin=cooldown['origin'],
                                              blocked_until=cooldown['blocked_until'].isoformat(),
                                              wait_source=cooldown['wait_source'],url=cooldown['url'])
                                         if cooldown else None)),
                    disk=dict(free_bytes=shutil.disk_usage(self.directory).free,
                              reserve_bytes=self.reserve_bytes),
                    downloads=dict(records_attempted=pdf_report.get('processed_records',0),
                                   works_with_pdf=pdf_report.get('works_with_candidate_pdf',
                                                                 pdf_report.get('statuses',{}).get('sucesso',0)),
                                   candidate_unique_pdfs=pdf_report.get('candidate_work_pdfs',0),
                                   all_unique_pdfs=pdf_report.get('unique_pdfs',0),
                                   known_site_documents=pdf_report.get('site_document_pdfs',0),
                                   possible_extra_candidates=pdf_report.get('possible_extra_candidate_files',0),
                                   bytes=pdf_report.get('pdf_bytes',0),
                                   failures=failure_counts),
                    partitions_summary=dict(collections.Counter(row['status'] for row in parts)),
                    inaccessible_partition_records=sum(row['inaccessible_records'] or 0 for row in parts),
                    split_coverage=split_coverage,
                    batches=batch_summary,
                    stop_reason=stop_reason,partitions=parts)
        atomic(self.directory/'reports/collection.json',json.dumps(report,ensure_ascii=False,indent=2))
        return report

    def run(self,contact,max_hours=8,budget_bytes=20*1024**3,max_records=None,
            rate_limit_wait_minutes=60,max_rate_limit_waits=4,
            import_metadata_paths=(),reuse_pdf_dirs=(),repository_retry_hours=24,
            annual_windows=False):
        started=time.monotonic(); deadline=started+max_hours*3600
        rate_limit_waits=0; pdf_report=None; attempted_run=0; stop_reason=None
        self.download_attempted_ids=set()
        self.repository_retry_seconds=repository_retry_hours*3600
        self.reuse_pdf_dirs=tuple(map(Path,reuse_pdf_dirs))
        if import_metadata_paths:
            self.import_metadata(import_metadata_paths)
        self.bootstrap_cooldown_from_log(rate_limit_wait_minutes*60)
        api_client=Client(f'BDTD-Academic-Crawler/{VERSION} (contato: {contact})',interval=10.0,
                          log=self.directory/'logs/api_http.jsonl',retry_base=15.0,
                          retry_429=False,robots_cache=self.directory/'cache/api_robots.json')
        pdf_client=StreamClient(contact,self.directory/'pdfs',max_bytes=100*1024**2,
                                budget=budget_bytes,reserve_bytes=self.reserve_bytes)
        builder=BatchBuilder(self.directory/'batches',self.directory/'pdfs',
                             self.reserve_bytes,self.batch_bytes)
        progress=TerminalProgress(interval=30)
        initial=self.report(pdf_report)
        progress.update_collection(initial)
        progress.start()

        def time_left():
            return deadline-time.monotonic()

        def drain_queue(stage):
            nonlocal attempted_run,pdf_report
            if time_left()<=0 or pdf_client.transferred>=pdf_client.budget:
                return 0
            remaining=None if max_records is None else max_records-attempted_run
            if remaining is not None and remaining<=0:
                return 0
            progress.stage_start(stage)
            try:
                per_cycle=min(50,remaining) if remaining is not None else 50
                attempted,pdf_report=self.download_available(contact,pdf_client,per_cycle,progress,deadline)
                attempted_run+=attempted
            finally:
                progress.stage_end(stage)
            if pdf_report:
                progress.stage_start('empacotamento')
                try:
                    builder.close_ready(self.directory/'pdfs/raw/manifest_pdf.jsonl',
                                        self.directory/'metadata/records.jsonl')
                finally:
                    progress.stage_end('empacotamento')
            current=self.report(pdf_report)
            progress.update_collection(current)
            return attempted

        def guarded(operation,phase):
            nonlocal rate_limit_waits
            while True:
                if time_left()<=0:
                    raise RateLimitStop('limite_de_tempo_da_execucao')
                cooldown=self.active_cooldown(self.config['endpoint'])
                if cooldown:
                    resume_at=cooldown['blocked_until'].astimezone()
                    progress.waiting('HTTP_429_persistido_'+phase,resume_at)
                    drain_queue('downloads_durante_espera_api')
                    while datetime.now(timezone.utc)<cooldown['blocked_until']:
                        if time_left()<=0:
                            raise RateLimitStop('limite_de_tempo_da_execucao_durante_espera_429')
                        remaining=(cooldown['blocked_until']-datetime.now(timezone.utc)).total_seconds()
                        pause=min(30,max(0,remaining),time_left())
                        if pause>0: time.sleep(pause)
                    progress.clear_wait()
                try:
                    value=operation()
                    self.clear_cooldown(self.config['endpoint'])
                    progress.clear_wait()
                    return value
                except urllib.error.HTTPError as exc:
                    if exc.code!=429:
                        raise
                    raw,wait_seconds,wait_source,evidence=rate_limit_policy(
                        exc,rate_limit_wait_minutes)
                    resume_mono=time.monotonic()+wait_seconds
                    resume_at=datetime.now().astimezone()+timedelta(seconds=wait_seconds)
                    self.record_rate_limit(exc.geturl(),raw or None,resume_at,wait_source,
                                           dict(evidence,error=str(exc),phase=phase))
                    exc.close()
                    if rate_limit_waits>=max_rate_limit_waits:
                        raise RateLimitStop(f'rate_limit_429_em_{phase}; retome mais tarde')
                    rate_limit_waits+=1
                    self.report(pdf_report,stop_reason=f'aguardando_rate_limit_429_{phase}')
                    progress.waiting(f'HTTP_429_{phase}',resume_at)
                    # A limitação é da BDTD. Repositórios independentes podem
                    # consumir a fila já persistida durante este intervalo.
                    drain_queue('downloads_durante_espera_api')
                    while time.monotonic()<resume_mono:
                        if time_left()<=0:
                            raise RateLimitStop('limite_de_tempo_da_execucao_durante_espera_429')
                        pause=min(30,resume_mono-time.monotonic(),time_left())
                        if pause>0: time.sleep(pause)

        try:
            drain_queue('fila_existente_inicial')
            current=self.report(pdf_report)
            if current['downloads']['candidate_unique_pdfs']>=self.target:
                stop_reason='meta_atingida'
            if stop_reason is None and annual_windows:
                progress.stage_start('planejamento_janelas_anuais')
                try:
                    self.plan_annual_windows()
                    self.import_archived_annual_pages(
                        after_page=lambda:drain_queue('downloads_pagina_arquivada'))
                finally: progress.stage_end('planejamento_janelas_anuais')
            if stop_reason is None and not annual_windows and not self.db.execute(
                    "SELECT 1 FROM validations WHERE name='partition_filters'").fetchone():
                progress.stage_start('validacao_api')
                try: guarded(lambda:self.validate_partitioning(api_client),'validacao')
                finally: progress.stage_end('validacao_api')
            if stop_reason is None and not annual_windows:
                progress.stage_start('planejamento_particoes')
                try: guarded(lambda:self.plan_root(api_client),'planejamento')
                finally: progress.stage_end('planejamento_particoes')
            while True:
                if stop_reason is not None: break
                if shutil.disk_usage(self.directory).free<=self.reserve_bytes:
                    stop_reason='reserva_de_disco'; break
                current=self.report(pdf_report)
                progress.update_collection(current)
                if current['downloads']['candidate_unique_pdfs']>=self.target:
                    stop_reason='meta_atingida'; break
                if time_left()<=0:
                    stop_reason='limite_de_tempo_da_execucao'; break
                remaining=None if max_records is None else max_records-attempted_run
                if remaining is not None and remaining<=0:
                    stop_reason='limite_de_registros_da_execucao'; break
                attempted=drain_queue('downloads_fila')
                if pdf_client.transferred>=pdf_client.budget:
                    stop_reason='orcamento_de_transferencia_da_execucao'; break
                current=self.report(pdf_report)
                progress.update_collection(current)
                if current['downloads']['candidate_unique_pdfs']>=self.target:
                    stop_reason='meta_atingida'; break
                try:
                    progress.stage_start('coleta_metadados')
                    operation=(lambda:self.harvest_annual_window(api_client)) if annual_windows \
                              else (lambda:self.harvest_one(api_client))
                    harvested=guarded(operation,'coleta_metadados')
                except RateLimitStop as exc:
                    stop_reason=str(exc); break
                finally:
                    progress.stage_end('coleta_metadados')
                attempted+=drain_queue('downloads_novos_metadados')
                if not harvested and not attempted:
                    stop_reason='particoes_e_fila_esgotadas'; break
                if pdf_client.transferred>=pdf_client.budget:
                    stop_reason='orcamento_de_transferencia_da_execucao'; break
            if stop_reason in ('meta_atingida','particoes_e_fila_esgotadas'):
                builder.close_ready(self.directory/'pdfs/raw/manifest_pdf.jsonl',
                                    self.directory/'metadata/records.jsonl',force=True)
            final=self.report(pdf_report,stop_reason)
            if annual_windows: self.annual_windows_report(stop_reason)
            progress.update_collection(final)
            return final
        except RateLimitStop as exc:
            stop_reason=str(exc)
            final=self.report(pdf_report,stop_reason)
            if annual_windows: self.annual_windows_report(stop_reason)
            progress.update_collection(final)
            return final
        finally:
            builder.db.close()
            progress.stop(stop_reason or 'erro_ou_interrupcao')


def main():
    parser=argparse.ArgumentParser(description='Coleta particionada e retomável de até 5.000 PDFs candidatos')
    parser.add_argument('--contact',required=True)
    parser.add_argument('--config',type=Path,default=Path('conf/api-validacao-linguagem-comunicacao.json'))
    parser.add_argument('--data-dir',type=Path,default=Path('data/coleta-5000'))
    parser.add_argument('--target-pdfs',type=int,default=5000)
    parser.add_argument('--reserve-gb',type=float,default=10.0)
    parser.add_argument('--batch-gb',type=float,default=1.0)
    parser.add_argument('--budget-gb',type=float,default=20.0)
    parser.add_argument('--max-hours',type=float,default=8.0)
    parser.add_argument('--rate-limit-wait-minutes',type=float,default=60.0)
    parser.add_argument('--max-rate-limit-waits',type=int,default=4)
    parser.add_argument('--max-records',type=int)
    parser.add_argument('--repository-retry-hours',type=float,default=24,
                        help='Intervalo mínimo antes de tentar novamente um registro concluído com falha')
    parser.add_argument('--import-metadata',type=Path,action='append',default=[],
                        help='JSONL anterior comprovadamente compatível; pode ser repetido')
    parser.add_argument('--reuse-pdf-dir',type=Path,action='append',default=[],
                        help='Diretório de download anterior com raw/manifest_pdf.jsonl; pode ser repetido')
    parser.add_argument('--validate-only',action='store_true')
    parser.add_argument('--validate-year',type=int,
                        help='Valida somente uma partição anual e encerra, sem coleta ou espera')
    parser.add_argument('--validate-institution',action='store_true',
                        help='No modo anual, acrescenta uma instituição escolhida das facetas arquivadas')
    parser.add_argument('--annual-windows',action='store_true',
                        help='Coleta janelas anuais de até 50 páginas, começando por 2024')
    args=parser.parse_args()
    if ('@' not in args.contact or '\\' in args.contact or
            any(char.isspace() for char in args.contact)):
        parser.error('Informe um e-mail válido.')
    if (not 1<=args.target_pdfs<=5000 or
        min(args.reserve_gb,args.batch_gb,args.budget_gb,args.max_hours,args.rate_limit_wait_minutes)<=0 or
        args.max_rate_limit_waits<0 or args.repository_retry_hours<0):
        parser.error('Limites devem ser positivos e --target-pdfs deve estar entre 1 e 5000.')
    if args.validate_only and args.validate_year is not None:
        parser.error('Use somente um entre --validate-only e --validate-year.')
    if args.validate_institution and args.validate_year is None:
        parser.error('--validate-institution exige --validate-year.')
    if args.annual_windows and (args.validate_only or args.validate_year is not None):
        parser.error('--annual-windows não pode ser combinado com modos de validação.')
    collector=None
    try:
        config=json.loads(args.config.read_text(encoding='utf-8'))
        collector=Collector(args.data_dir,config,args.target_pdfs,
                            int(args.reserve_gb*1024**3),int(args.batch_gb*1024**3))
        if args.validate_year is not None:
            cooldown=collector.active_cooldown(config['endpoint'])
            if cooldown:
                print('Cooldown ativo; retome após',cooldown['blocked_until'].astimezone().isoformat(),flush=True)
            else:
                selected=collector.select_archived_institution() if args.validate_institution else None
                criteria=[year_criterion(args.validate_year)]
                if selected:
                    criteria.append(quoted_filter('institution',selected['value']))
                client=Client(f'BDTD-Academic-Crawler/{VERSION} (contato: {args.contact})',interval=10.0,
                              log=args.data_dir/'logs/api_http.jsonl',retry_429=False,max_attempts=1,
                              robots_cache=args.data_dir/'cache/api_robots.json')
                obj,sha,url=collector.request(client,criteria,limit=20)
                report=dict(version=VERSION,validated_at=now(),year=args.validate_year,
                            institution=selected,result_count=obj['resultCount'],records=len(obj['records']),
                            ids=[row['id'] for row in obj['records']],
                            institution_fields=[dict(record_id=row['id'],fields=collector.institution_fields(row))
                                                for row in obj['records']],
                            sha256=sha,url=url,source=collector.last_api_source)
                suffix='_institution' if selected else ''
                atomic(args.data_dir/f'reports/year_{args.validate_year}{suffix}_validation.json',
                       json.dumps(report,ensure_ascii=False,indent=2))
                print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
        elif args.validate_only:
            client=Client(f'BDTD-Academic-Crawler/{VERSION} (contato: {args.contact})',interval=10.0,
                          log=args.data_dir/'logs/api_http.jsonl',retry_base=15.0,retry_429=False,
                          robots_cache=args.data_dir/'cache/api_robots.json')
            report=collector.validate_partitioning(client)
            print('Particionamento validado:',args.data_dir/'reports/partition_validation.json')
            print(json.dumps(report,ensure_ascii=False,indent=2))
        else:
            report=collector.run(args.contact,args.max_hours,int(args.budget_gb*1024**3),
                                 args.max_records,args.rate_limit_wait_minutes,args.max_rate_limit_waits,
                                 args.import_metadata,args.reuse_pdf_dir,args.repository_retry_hours,
                                 args.annual_windows)
            print('Coleta encerrada de forma retomável:',report['stop_reason'])
            print('Relatório:',(args.data_dir/'reports/collection.json').resolve())
    except KeyboardInterrupt:
        if collector: collector.report(stop_reason='interrompido_pelo_usuario')
        parser.exit(130,'Interrompido; resultados concluídos foram preservados.\n')
    except Exception as exc:
        if collector: collector.report(stop_reason='erro: '+str(exc))
        parser.exit(1,f'Interrompido: {exc}\n')
    finally:
        if collector: collector.db.close()


if __name__=='__main__': main()
