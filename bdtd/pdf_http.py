"""HTTP em streaming, redirecionamentos limitados e robots por origem."""
import hashlib
import json
import os
import random
import ssl
import tempfile
import time
import uuid
from pathlib import Path
from email.utils import parsedate_to_datetime
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit, quote
from urllib.request import Request
from urllib.robotparser import RobotFileParser
from .core import now
from .network import Client

class FetchError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def failure(code, message, url, stage, cause, http_status=None, **details):
    return FetchError(code, message, dict(stage=stage, request_url=url,
        http_status=http_status, cause=cause, **details))


def clean_url(url):
    p = urlsplit(url.strip())
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise FetchError('url_invalida', 'URL HTTP(S) sem credenciais necessária.')
    return urlunsplit((p.scheme, p.netloc, quote(p.path, safe='/%:@!$&\'()*+,;=-._~'),
                       quote(p.query, safe='/%?:@!$&\'()*+,;=-._~'), ''))

class StreamClient(Client):
    def __init__(self, contact, directory, max_bytes=100*1024**2, budget=500*1024**2):
        super().__init__(f'BDTD-Academic-Crawler/0.3.2 (contato: {contact})', log=Path(directory)/'logs/pdf_http.jsonl')
        self.directory = Path(directory)
        self.max_bytes, self.budget, self.transferred = max_bytes, budget, 0
        (self.directory/'tmp').mkdir(parents=True, exist_ok=True)
        self.robots_delays = {}
        self.policy_failures = {}
        self.policy_details = {}
        self.run_id = uuid.uuid4().hex
        self.record_id = None

    def _check_budget(self, url, stage):
        if self.transferred >= self.budget:
            raise failure('limite_execucao', 'Limite de transferência desta execução atingido.',
                          url, stage, 'execution_limit')

    def _once(self, url, ceiling, stage='resource', attempt=1):
        self._check_budget(url, stage)
        host=urlsplit(url).netloc
        delay=max(1, self.robots_delays.get(host, 1))-(time.monotonic()-self.last.get(host,0))
        if delay>60:
            raise failure('espera_dominio', f'Regras do domínio exigem aguardar {delay:.0f}s; retome depois.',
                          url, stage, 'crawl_delay')
        if delay>0: time.sleep(delay)
        self.last[host]=time.monotonic()
        fd, name=tempfile.mkstemp(dir=self.directory/'tmp', suffix='.part')
        os.close(fd)
        path=Path(name)
        status, size, error, response_sha=None, 0, None, None
        try:
            req=Request(url, headers={'User-Agent':self.agent, 'Accept-Encoding':'identity'})
            with self.opener.open(req, timeout=self.timeout) as r, path.open('wb') as f:
                status=r.status
                if status != 200:
                    raise FetchError('http_inesperado', f'HTTP {status}; esperado 200 completo.')
                expected=r.headers.get('Content-Length')
                expected=int(expected) if expected and expected.isdigit() else None
                if expected is not None and expected>ceiling:
                    raise FetchError('limite_arquivo', 'Content-Length excede o limite por resposta.')
                if expected is not None and expected>self.budget-self.transferred:
                    raise FetchError('limite_execucao', 'Content-Length excede o saldo de transferência desta execução.')
                sha=hashlib.sha256()
                while True:
                    # Com tamanho conhecido não é necessário ler além do corpo.
                    if expected is not None and size == expected: break
                    self._check_budget(url, stage)
                    block=r.read(min(65536, ceiling-size+1, self.budget-self.transferred))
                    if not block: break
                    size+=len(block)
                    self.transferred+=len(block)
                    if size>ceiling:
                        raise FetchError('limite_arquivo', 'Resposta excedeu o limite por arquivo.')
                    f.write(block)
                    sha.update(block)
                if expected is not None and expected != size:
                    raise FetchError('download_incompleto', 'Bytes recebidos diferem de Content-Length.')
                response_sha=sha.hexdigest()
                return dict(path=str(path), sha256=response_sha, bytes=size,
                            content_type=r.headers.get_content_type(),
                            charset=r.headers.get_content_charset() or 'utf-8', url=url,
                            http_status=status)
        except BaseException as exc:
            error=str(exc)
            if isinstance(exc, HTTPError): status=exc.code
            path.unlink(missing_ok=True)
            if isinstance(exc, FetchError):
                exc.details.setdefault('stage', stage)
                exc.details.setdefault('request_url', url)
                exc.details.setdefault('http_status', status)
                exc.details.setdefault('cause', exc.code)
            elif isinstance(exc, (TimeoutError, URLError, ConnectionError, HTTPException)) and not isinstance(exc, HTTPError):
                reason=exc.reason if isinstance(exc, URLError) else exc
                cause=('timeout' if isinstance(reason, TimeoutError) else
                       'tls_verification' if isinstance(reason, ssl.SSLCertVerificationError) else 'network')
                code='timeout' if cause=='timeout' else 'erro_rede'
                if isinstance(exc, IncompleteRead):
                    cause, code='response_incomplete', 'download_incompleto'
                raise failure(code, str(exc), url, stage, cause, status) from exc
            raise
        finally:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open('a', encoding='utf-8') as f:
                entry=dict(ts=now(),run_id=self.run_id,url=url,status=status,bytes=size,
                           error=error,stage=stage,attempt=attempt)
                if self.record_id: entry['record_id']=self.record_id
                if response_sha: entry['sha256']=response_sha
                p=urlsplit(url)
                if stage=='resource': entry.update(self.policy_details.get(p.scheme+'://'+p.netloc, {}))
                f.write(json.dumps(entry, ensure_ascii=False)+'\n')

    def _transfer(self, url, ceiling, stage='resource'):
        for attempt in range(3):
            try: return self._once(url, ceiling, stage=stage, attempt=attempt+1)
            except HTTPError as exc:
                try:
                    if exc.code not in (429,500,502,503,504) or attempt==2: raise
                    raw=exc.headers.get('Retry-After','0')
                    try: wait=float(raw)
                    except ValueError:
                        try: wait=max(0,parsedate_to_datetime(raw).timestamp()-time.time())
                        except (ValueError, TypeError, OverflowError): wait=0
                    if wait>60:
                        raise failure('retry_after', f'Servidor pediu espera de {wait:.0f}s; retome depois.',
                                      url, stage, 'retry_after', exc.code) from exc
                finally:
                    # Os headers permanecem disponíveis aos tratadores de redirect.
                    exc.close()
                time.sleep(max(wait, 2**attempt+random.random()))

    def _policy(self, url):
        self._check_budget(url, 'robots')
        p=urlsplit(url)
        origin=p.scheme+'://'+p.netloc
        if origin in self.policy_failures:
            code, message, details=self.policy_failures[origin]
            raise FetchError(code, message, dict(details, policy_cached=True))
        cached=origin in self.rules
        if origin not in self.rules:
            target=origin+'/robots.txt'
            visited=set()
            evidence={}
            try:
                for _ in range(6):
                    if target in visited:
                        raise failure('robots_indisponivel','Loop no robots.txt.',target,'robots','redirect_loop')
                    visited.add(target)
                    try:
                        result=self._transfer(target, 1024*1024, stage='robots')
                        path=Path(result['path'])
                        body=path.read_bytes()
                        # Objetos anteriores não são substituídos, mesmo na retomada.
                        relative=Path('raw/robots')/(result['sha256']+'.txt')
                        dest=self.directory/relative
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if dest.exists():
                            if hashlib.sha256(dest.read_bytes()).hexdigest()!=result['sha256']:
                                path.unlink(missing_ok=True)
                                raise ValueError('Objeto Raw de robots existente corrompido.')
                            path.unlink()
                        else:
                            os.replace(path,dest)
                        evidence=dict(robots_url=target,robots_http_status=200,
                                      robots_sha256=result['sha256'],robots_path=relative.as_posix())
                        text=body.decode('utf-8', errors='replace')
                        if '<html' in text.lower() or '<!doctype html' in text.lower():
                            raise failure('robots_indisponivel','robots.txt retornou HTML.',
                                          target,'robots','invalid_policy',200,**evidence)
                        break
                    except HTTPError as exc:
                        try:
                            if exc.code in (301,302,303,307,308) and exc.headers.get('Location'):
                                try: target=clean_url(urljoin(target,exc.headers['Location']))
                                except (FetchError, ValueError) as invalid:
                                    raise failure('robots_indisponivel','Redirect inválido no robots.txt.',
                                                  target,'robots','invalid_redirect',exc.code) from invalid
                                continue
                            if exc.code==404:
                                evidence=dict(robots_url=target,robots_http_status=404)
                                text=''
                                break
                            raise failure('robots_indisponivel',f'robots.txt retornou HTTP {exc.code}.',
                                          target,'robots','http',exc.code) from exc
                        finally:
                            exc.close()
                else:
                    raise failure('robots_indisponivel','Muitos redirecionamentos no robots.txt.',
                                  target,'robots','redirect_limit')
            except FetchError as exc:
                if exc.code in ('timeout','erro_rede','download_incompleto','http_inesperado'):
                    exc=FetchError('robots_indisponivel',str(exc),exc.details)
                exc.details.setdefault('stage','robots')
                exc.details.setdefault('request_url',target)
                exc.details.setdefault('http_status',None)
                exc.details.setdefault('cause',exc.code)
                exc.details['policy_cached']=False
                if exc.code in ('robots_indisponivel','retry_after','espera_dominio'):
                    # Falhas valem apenas para este cliente, nunca persistem como permissão.
                    self.policy_failures[origin]=(exc.code,str(exc),dict(exc.details))
                raise exc
            parser=RobotFileParser()
            parser.parse(text.splitlines())
            self.rules[origin]=parser
            self.policy_details[origin]=evidence
            delay=parser.crawl_delay(self.agent) or 1
            rate=parser.request_rate(self.agent)
            if rate and rate.requests: delay=max(delay,rate.seconds/rate.requests)
            self.robots_delays[p.netloc]=max(1,delay)
            policy_url=urlsplit(evidence.get('robots_url',''))
            policy_origin=policy_url.scheme+'://'+policy_url.netloc
            if policy_url.path=='/robots.txt' and policy_origin!=origin:
                # Um redirect confirmado para o robots canônico também confirma
                # a política da origem de destino durante esta execução.
                self.rules.setdefault(policy_origin,parser)
                self.policy_details.setdefault(policy_origin,evidence)
                self.robots_delays.setdefault(policy_url.netloc,max(1,delay))
        if not self.rules[origin].can_fetch(self.agent,url):
            raise failure('robots_bloqueado','robots.txt não permite esta URL.',
                          self.policy_details[origin]['robots_url'],'robots','disallow',
                          self.policy_details[origin]['robots_http_status'],
                          policy_cached=cached,**self.policy_details[origin])
        return dict(self.policy_details[origin],policy_cached=cached)

    def fetch(self, url):
        visited=set()
        url=clean_url(url)
        for _ in range(9):
            self._check_budget(url, 'resource')
            if url in visited:
                raise failure('redirect_loop','Loop de redirecionamento.',url,'resource','redirect_loop')
            visited.add(url)
            policy=self._policy(url)
            policy=policy if isinstance(policy,dict) else {}
            try:
                result=self._transfer(url,self.max_bytes)
                if policy: result['robots_policy']=policy
                return result
            except HTTPError as exc:
                try:
                    if exc.code in (301,302,303,307,308) and exc.headers.get('Location'):
                        url=clean_url(urljoin(url,exc.headers['Location']))
                        continue
                    raise failure('http_'+str(exc.code), f'HTTP {exc.code} em {url}',
                                  url,'resource','http',exc.code,**policy) from exc
                finally:
                    exc.close()
            except FetchError as exc:
                exc.details.update(policy)
                raise
        raise failure('redirect_limite','Mais de oito redirecionamentos.',url,'resource','redirect_limit')
