"""Cliente conservador. Não resolve desafios nem segue redirects automaticamente."""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from email.utils import parsedate_to_datetime
from .core import atomic, digest, now, parse_oai

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Client:
    def __init__(self, user_agent, interval=1.0, timeout=25, log=None):
        self.agent = user_agent
        self.interval = max(1.0, interval)
        self.timeout = timeout
        self.last = {}
        self.rules = {}
        self.log = log
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('URL HTTP(S) necessária.')
        for attempt in range(4):
            delay = self.interval - (time.monotonic() - self.last.get(parts.netloc, 0))
            if delay > 0:
                time.sleep(delay)
            self.last[parts.netloc] = time.monotonic()
            status, error, size = None, None, 0
            try:
                req = urllib.request.Request(url, headers={'User-Agent': self.agent})
                with self.opener.open(req, timeout=self.timeout) as response:
                    status = response.status
                    body = response.read(20 * 1024 * 1024 + 1)
                    size = len(body)
                    if size > 20 * 1024 * 1024:
                        raise ValueError('Resposta excede 20 MiB; interrompida.')
                    return body
            except urllib.error.HTTPError as exc:
                status, error = exc.code, str(exc)
                if status not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
                retry = exc.headers.get('Retry-After', '0')
                try:
                    wait = float(retry)
                except ValueError:
                    wait = max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                if wait > 60:
                    raise RuntimeError(f'Servidor pediu espera de {wait:.0f}s. Retome mais tarde.') from exc
                time.sleep(max(wait, 2 ** attempt + random.random()))
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                if self.log:
                    self.log.parent.mkdir(parents=True, exist_ok=True)
                    with self.log.open('a', encoding='utf-8') as f:
                        f.write(json.dumps(dict(ts=now(), url=url, status=status, attempt=attempt+1,
                                                bytes=size, error=error), ensure_ascii=False)+'\n')

    def get(self, url):
        parts = urllib.parse.urlsplit(url)
        origin = parts.scheme + '://' + parts.netloc
        if origin not in self.rules:
            robots_url = origin + '/robots.txt'
            try:
                text = self.request(robots_url).decode('utf-8', errors='replace')
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                text = ''
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

def query(endpoint, params):
    return endpoint + ('&' if '?' in endpoint else '?') + urllib.parse.urlencode(params)

def harvest(store, client, endpoint, set_spec, max_pages):
    # Uma coleta é identificada pelo endpoint e pelo set confirmado.
    job = digest((endpoint + '\n' + set_spec).encode())
    previous = store.db.execute('SELECT token,done FROM jobs WHERE id=?', (job,)).fetchone()
    token, done = previous or ('', 0)
    if done:
        return 0
    pages = 0
    for _ in range(max_pages):
        params = {'verb': 'ListRecords', 'resumptionToken': token} if token else {
            'verb': 'ListRecords', 'metadataPrefix': 'oai_dc', 'set': set_spec}
        url = query(endpoint, params)
        data = client.get(url)
        root = parse_oai(data, 'ListRecords')
        next_token = root.findtext('.//{http://www.openarchives.org/OAI/2.0/}resumptionToken', '').strip()
        if token and next_token == token:
            raise ValueError('Token repetido: paginação não avançou.')
        store.archive(data, url)
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?)', (job, next_token, int(not next_token)))
        pages += 1
        token = next_token
        if not token:
            break
    store.export()
    return pages

def spike(client, config, directory):
    results = []
    for item in config['candidates']:
        result = {'url': item['url'], 'kind': item['kind'], 'checked_at': now()}
        try:
            data = client.get(item['url'])
            if item['kind'] == 'oai':
                parse_oai(data, item['verb'])
            elif item['kind'] == 'api':
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    raise ValueError('API não retornou objeto JSON.')
            else:
                if b'verificando' in data.lower() or b'challenge' in data.lower():
                    raise ValueError('Página de verificação; conteúdo não confirmado.')
            result.update(status='resposta_valida', bytes=len(data))
            atomic(directory / 'diagnostics' / (digest(item['url'].encode()) + '.txt'), data.decode('utf-8', errors='replace'))
        except Exception as exc:
            result.update(status='nao_confirmado', reason=str(exc))
        results.append(result)
    atomic(directory / 'reports' / 'spike.json', json.dumps(results, ensure_ascii=False, indent=2))
    return results
