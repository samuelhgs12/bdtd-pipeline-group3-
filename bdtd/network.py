"""Cliente conservador. Não resolve desafios nem segue redirects automaticamente."""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from email.utils import parsedate_to_datetime
from pathlib import Path
from .core import atomic, digest, now

ERROR_BODY_LIMIT = 64 * 1024


def _error_evidence(exc):
    """Extrai somente metadados seguros e um trecho limitado do corpo HTTP."""
    headers = exc.headers or {}
    body = exc.read(ERROR_BODY_LIMIT + 1)
    truncated = len(body) > ERROR_BODY_LIMIT
    body = body[:ERROR_BODY_LIMIT]
    content_type = headers.get('Content-Type')
    charset = headers.get_content_charset() if hasattr(headers, 'get_content_charset') else None
    text = body.decode(charset or 'utf-8', errors='replace')
    # Mantém o JSONL legível e evita registrar respostas arbitrariamente grandes.
    excerpt = ''.join(char if char in '\n\r\t' or ord(char) >= 32 else ' ' for char in text[:4096])
    evidence = dict(retry_after=headers.get('Retry-After'), content_type=content_type,
                response_date=headers.get('Date'), server=headers.get('Server'),
                response_body_bytes=len(body), body_truncated=truncated,
                body_excerpt=excerpt or None)
    return evidence, body

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Client:
    def __init__(self, user_agent, interval=1.0, timeout=25, log=None, retry_base=1.0,
                 retry_429=True,robots_cache=None,robots_ttl=24*3600,max_attempts=4):
        self.agent = user_agent
        self.interval = max(1.0, interval)
        self.timeout = timeout
        self.retry_base = max(1.0,retry_base)
        self.retry_429 = retry_429
        self.last = {}
        self.rules = {}
        self.log = log
        self.robots_cache = Path(robots_cache) if robots_cache else None
        self.robots_ttl = robots_ttl
        self.max_attempts = max(1, int(max_attempts))
        self.opener = urllib.request.build_opener(NoRedirect())
        self.last_response = None

    def log_event(self,event):
        if not self.log: return
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open('a', encoding='utf-8') as f:
            f.write(json.dumps(event,ensure_ascii=False)+'\n')

    def request(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('URL HTTP(S) necessária.')
        for attempt in range(self.max_attempts):
            delay = self.interval - (time.monotonic() - self.last.get(parts.netloc, 0))
            if delay > 0:
                time.sleep(delay)
            self.last[parts.netloc] = time.monotonic()
            status, error, size, evidence = None, None, 0, {}
            try:
                req = urllib.request.Request(url, headers={'User-Agent': self.agent})
                with self.opener.open(req, timeout=self.timeout) as response:
                    status = response.status
                    body = response.read(20 * 1024 * 1024 + 1)
                    size = len(body)
                    if size > 20 * 1024 * 1024:
                        raise ValueError('Resposta excede 20 MiB; interrompida.')
                    evidence['sha256']=digest(body)
                    self.last_response = dict(url=response.geturl(), status=status,
                                              headers=dict(response.headers.items()), body=body)
                    return body
            except urllib.error.HTTPError as exc:
                status, error = exc.code, str(exc)
                evidence, error_body = _error_evidence(exc)
                size = evidence['response_body_bytes']
                # O controlador usa a mesma evidência sem voltar a consumir o corpo.
                exc.bdtd_evidence = dict(evidence)
                exc.bdtd_response_body = error_body
                if status not in (429, 500, 502, 503, 504) or attempt == self.max_attempts - 1:
                    raise
                if status == 429 and not self.retry_429:
                    raise
                retry = exc.headers.get('Retry-After', '0')
                try:
                    wait = float(retry)
                except ValueError:
                    wait = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                if wait > 60:
                    raise RuntimeError(f'Servidor pediu espera de {wait:.0f}s. Retome mais tarde.') from exc
                time.sleep(min(60,max(wait, self.retry_base * 2 ** attempt + random.random())))
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                if self.log:
                    self.log_event(dict(ts=now(), url=url, status=status, attempt=attempt+1,
                                        bytes=size, error=error, source='network',**evidence))

    def get(self, url):
        parts = urllib.parse.urlsplit(url)
        origin = parts.scheme + '://' + parts.netloc
        if origin not in self.rules:
            robots_url = origin + '/robots.txt'
            text=None
            if self.robots_cache and self.robots_cache.exists():
                try:
                    cache=json.loads(self.robots_cache.read_text(encoding='utf-8'))
                    saved=cache.get(origin,{})
                    if time.time()-saved.get('fetched_epoch',0)<=self.robots_ttl:
                        text=saved['text']
                        self.log_event(dict(ts=now(),url=robots_url,status='cache_hit',attempt=0,
                                            bytes=len(text.encode('utf-8')),error=None,source='cache',
                                            cache_kind='robots'))
                except (OSError,ValueError,KeyError,TypeError):
                    text=None
            if text is None:
                try:
                    text = self.request(robots_url).decode('utf-8', errors='replace')
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                    text = ''
                if self.robots_cache:
                    try: cache=json.loads(self.robots_cache.read_text(encoding='utf-8'))
                    except (OSError,ValueError,TypeError): cache={}
                    cache[origin]=dict(text=text,fetched_epoch=time.time(),fetched_at=now(),url=robots_url)
                    atomic(self.robots_cache,json.dumps(cache,ensure_ascii=False,indent=2))
            if '<html' in text.lower() or '<!doctype html' in text.lower():
                raise ValueError('robots.txt retornou HTML; acesso não confirmado.')
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(text.splitlines())
            self.rules[origin] = parser
        parser = self.rules[origin]
        if not parser.can_fetch(self.agent, url):
            raise PermissionError('robots.txt impede acesso: ' + url)
        self.interval = max(self.interval, parser.crawl_delay(self.agent) or 0)
        return self.request(url)
