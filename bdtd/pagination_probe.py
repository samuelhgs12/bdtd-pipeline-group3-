"""Diagnóstico pequeno e retomável dos limites de paginação da API BDTD."""
import argparse
import json
import sqlite3
from pathlib import Path

from .api import build_url, validate
from .core import atomic, digest, now
from .network import Client

VERSION = '0.4.0'
DEFAULT_CASES = (
    (1, 1), (1, 2), (1, 50), (1, 100), (1, 101), (1, 500),
    (1, 999), (1, 1000), (1, 1001), (1, 2000), (1, 5000), (1, 8000),
    (1, 10000), (1, 10001),
    (20, 1), (20, 2), (20, 50), (20, 100), (20, 101),
    (20, 49), (20, 50), (20, 51), (20, 100), (20, 101), (20, 500), (20, 501),
    (100, 1), (100, 2), (100, 9), (100, 10), (100, 11),
    (100, 50), (100, 100), (100, 101),
)


class PaginationProbe:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.signature = digest(json.dumps(config, sort_keys=True, ensure_ascii=False).encode())
        self.db = sqlite3.connect(self.directory / 'pagination_probe.sqlite')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, signature TEXT);
          CREATE TABLE IF NOT EXISTS probes (
            requested_limit INTEGER, page INTEGER, response_total INTEGER,
            response_count INTEGER, sha TEXT, url TEXT, ts TEXT, payload TEXT,
            PRIMARY KEY(requested_limit, page));
        ''')
        previous = self.db.execute('SELECT signature FROM settings WHERE id=1').fetchone()
        if previous and previous[0] != self.signature:
            self.db.close()
            raise ValueError('Consulta mudou. Use outro --data-dir para preservar o diagnóstico anterior.')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (self.signature,))

    def archive(self, data):
        sha = digest(data)
        path = self.directory / 'raw/api' / f'{sha}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and digest(path.read_bytes()) != sha:
            raise ValueError('Raw do diagnóstico corrompido.')
        if not path.exists():
            path.write_bytes(data)
        return sha

    def run(self, client, cases=DEFAULT_CASES):
        for requested_limit, page in cases:
            if self.db.execute('SELECT 1 FROM probes WHERE requested_limit=? AND page=?',
                               (requested_limit, page)).fetchone():
                continue
            url = build_url(self.config, page, limit=requested_limit)
            data = client.get(url)
            sha = self.archive(data)
            obj = validate(data)
            ts = now()
            with self.db:
                self.db.execute('INSERT INTO probes VALUES(?,?,?,?,?,?,?,?)',
                                (requested_limit, page, obj['resultCount'], len(obj['records']),
                                 sha, url, ts, json.dumps(obj, ensure_ascii=False)))
            first = obj['records'][0]['id'] if obj['records'] else '-'
            print(f'limit={requested_limit} page={page}: {len(obj["records"])} registros; primeiro={first}',
                  flush=True)
            self.export()
        return self.export()

    def export(self, error=None):
        rows = []
        for row in self.db.execute('''SELECT requested_limit,page,response_total,response_count,
                                             sha,url,ts,payload
                                      FROM probes ORDER BY requested_limit,page'''):
            payload = json.loads(row[7])
            ids = [record['id'] for record in payload['records']]
            rows.append(dict(requested_limit=row[0], page=row[1], api_total=row[2],
                             response_count=row[3], sha256=row[4], url=row[5],
                             harvest_ts=row[6], first_id=ids[0] if ids else None,
                             last_id=ids[-1] if ids else None, record_ids=ids))
        seen = {}
        for row in rows:
            key = (row['requested_limit'], row['sha256'])
            row['identical_to_page'] = seen.get(key)
            seen.setdefault(key, row['page'])
        totals = sorted({row['api_total'] for row in rows})
        report = dict(version=VERSION, query_id=self.signature, query=self.config,
                      generated_at=now(), probes=len(rows), api_totals=totals,
                      error=error, cases=rows,
                      interpretation='Diagnóstico; a janela alcançável só deve ser declarada após comparar IDs e hashes.')
        atomic(self.directory / 'reports/pagination_probe.json',
               json.dumps(report, ensure_ascii=False, indent=2))
        manifest = [{k: v for k, v in row.items() if k != 'record_ids'} for row in rows]
        atomic(self.directory / 'raw/api_manifest.jsonl',
               ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in manifest))
        return report


def main():
    parser = argparse.ArgumentParser(description='Diagnóstico limitado da paginação da API BDTD')
    parser.add_argument('--contact', required=True)
    parser.add_argument('--config', type=Path,
                        default=Path('conf/api-validacao-linguagem-comunicacao.json'))
    parser.add_argument('--data-dir', type=Path,
                        default=Path('data/validacao-linguagem-comunicacao/pagination-probe'))
    args = parser.parse_args()
    if '@' not in args.contact or any(c.isspace() for c in args.contact):
        parser.error('Informe um e-mail válido.')
    probe = None
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        probe = PaginationProbe(args.data_dir, config)
        client = Client(f'BDTD-Academic-Crawler/{VERSION} (contato: {args.contact})',
                        log=args.data_dir / 'logs/http.jsonl')
        report = probe.run(client)
        print('Diagnóstico:', (args.data_dir / 'reports/pagination_probe.json').resolve())
        print('Casos:', report['probes'])
    except Exception as exc:
        if probe:
            probe.export(error=str(exc))
        parser.exit(1, f'Interrompido: {exc}\n')
    finally:
        if probe:
            probe.db.close()


if __name__ == '__main__':
    main()
