"""Executa uma única consulta diagnóstica, sem espera nem retry automático."""
import argparse
import json
import sqlite3
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import build_url, validate
from .core import atomic, digest, now
from .network import Client
from .collect import Collector, rate_limit_policy


RELEVANT_HEADERS = ('Retry-After', 'Content-Type', 'Date', 'Server')


def diagnostic_config(config):
    result = dict(config)
    result.update(limit=20, facets=[], date_ranges=['publishDate'],
                  publishDatefrom='2024', publishDateto='2024')
    return result


def active_cooldown(database, endpoint):
    if not database.exists():
        return None
    origin = '{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(endpoint))
    connection = sqlite3.connect(database)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_cooldowns'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute(
            'SELECT blocked_until,wait_source,url FROM api_cooldowns WHERE origin=?', (origin,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    deadline = datetime.fromisoformat(row[0])
    if deadline <= datetime.now(timezone.utc):
        return None
    return dict(origin=origin, blocked_until=deadline.isoformat(), wait_source=row[1], url=row[2])


def safe_headers(headers):
    return {name: headers.get(name) for name in RELEVANT_HEADERS if headers.get(name) is not None}


def report_path(directory, page):
    return directory / 'reports' / f'api_diagnostic_2024_page_{page}.json'


def archived_response(directory, url, page):
    candidates=[report_path(directory,page)]
    if page==1:
        candidates.append(directory/'reports/api_diagnostic_2024.json')
    for path in candidates:
        if not path.exists():
            continue
        try:
            report=json.loads(path.read_text(encoding='utf-8'))
            relative=report['response']['path']
            response_path=(directory/relative).resolve()
            if report.get('request_url')==url and directory.resolve() in response_path.parents:
                body=response_path.read_bytes()
                if digest(body)==report['response']['sha256']:
                    return report,body
        except (OSError,ValueError,KeyError,TypeError):
            continue
    return None


def page_one_ids(directory, config):
    url=build_url(config,page=1,limit=20)
    archived=archived_response(directory,url,1)
    if not archived:
        return None
    return [row['id'] for row in validate(archived[1])['records']]


def add_response_summary(report,body,first_ids):
    obj=validate(body)
    ids=[row['id'] for row in obj['records']]
    report.update(result_count=obj['resultCount'],records=len(ids),ids=ids,
                  page_1_ids_available=first_ids is not None,
                  overlap_with_page_1=([record_id for record_id in ids if record_id in set(first_ids)]
                                       if first_ids is not None else None))


def save(directory, report, page, body=None):
    reports = directory / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    if body is not None:
        sha = digest(body)
        raw = directory / 'raw' / 'diagnostic'
        raw.mkdir(parents=True, exist_ok=True)
        response_path = raw / f'{sha}.response'
        if not response_path.exists():
            response_path.write_bytes(body)
        report['response'] = dict(path=response_path.relative_to(directory).as_posix(),
                                  sha256=sha, bytes=len(body))
    atomic(report_path(directory,page), json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description='Diagnóstico único da consulta BDTD no ano de 2024')
    parser.add_argument('--contact', required=True)
    parser.add_argument('--config', type=Path,
                        default=Path('conf/api-validacao-linguagem-comunicacao.json'))
    parser.add_argument('--data-dir', type=Path, default=Path('data/coleta-5000'))
    parser.add_argument('--page',type=int,default=1)
    args = parser.parse_args()
    if '@' not in args.contact or any(char.isspace() for char in args.contact):
        parser.error('Informe um e-mail de contato válido.')
    if args.page<1:
        parser.error('--page deve ser positivo.')

    base_config=json.loads(args.config.read_text(encoding='utf-8'))
    state=Collector(args.data_dir,base_config)
    config = diagnostic_config(base_config)
    url = build_url(config, page=args.page, limit=20)
    report = dict(created_at=now(),page=args.page,request_url=url, method='GET', attempts=0,
                  source='none', status=None, headers={}, cooldown=None, error=None)
    cooldown = state.active_cooldown(config['endpoint'])
    if cooldown:
        report['cooldown'] = dict(cooldown,blocked_until=cooldown['blocked_until'].isoformat())
        report['error'] = 'cooldown_ativo_sem_requisicao'
        save(args.data_dir, report,args.page)
        print(f'Cooldown ativo; retome após {cooldown["blocked_until"].isoformat()}.', flush=True)
        state.db.close()
        return


    first_ids=page_one_ids(args.data_dir,config)
    archived=archived_response(args.data_dir,url,args.page)
    if archived:
        prior,body=archived
        report.update(source='cache',status=prior.get('status'),headers=prior.get('headers',{}))
        add_response_summary(report,body,first_ids)
        save(args.data_dir,report,args.page,body)
        print(json.dumps({key:report[key] for key in ('status','source','result_count','records','ids',
                                                       'overlap_with_page_1')},ensure_ascii=False,indent=2),flush=True)
        state.db.close()
        return

    client = Client(f'BDTD-Academic-Crawler/0.4 (contato: {args.contact})',
                    log=args.data_dir / 'logs' / 'api_http.jsonl', retry_429=False,
                    robots_cache=args.data_dir / 'cache' / 'api_robots.json', max_attempts=1)
    try:
        body = client.get(url)
        response = client.last_response or {}
        report.update(attempts=1, source='network', status=response.get('status'),
                      headers=safe_headers(response.get('headers', {})))
        add_response_summary(report,body,first_ids)
        report['cooldown_cleared']=state.clear_cooldown(config['endpoint'],datetime.now(timezone.utc))
        save(args.data_dir, report,args.page, body)
        print(json.dumps({key:report[key] for key in ('status','source','result_count','records','ids',
                                                       'overlap_with_page_1')},ensure_ascii=False,indent=2),flush=True)
    except urllib.error.HTTPError as exc:
        evidence = getattr(exc, 'bdtd_evidence', {})
        headers = {name: evidence.get(key) for name, key in (
            ('Retry-After', 'retry_after'), ('Content-Type', 'content_type'),
            ('Date', 'response_date'), ('Server', 'server')) if evidence.get(key) is not None}
        report.update(attempts=1, source='network', status=exc.code, headers=headers,
                      error=str(exc), body_truncated=evidence.get('body_truncated', False))
        if exc.code==429:
            raw,wait_seconds,wait_source,evidence=rate_limit_policy(exc,60)
            resume_at=datetime.now(timezone.utc)+timedelta(seconds=wait_seconds)
            state.record_rate_limit(exc.geturl(),raw or None,resume_at,wait_source,
                                    dict(evidence,error=str(exc),phase='diagnose_api'))
        save(args.data_dir, report,args.page, getattr(exc, 'bdtd_response_body', b''))
        print(json.dumps({key:report.get(key) for key in ('status','source','result_count','records',
                                                           'ids','overlap_with_page_1','error')},
                         ensure_ascii=False,indent=2),flush=True)
    except Exception as exc:
        report['error'] = str(exc)
        save(args.data_dir, report,args.page)
        raise
    finally:
        state.db.close()
        print(f'Relatório: {report_path(args.data_dir,args.page).resolve()}', flush=True)


if __name__ == '__main__':
    main()
