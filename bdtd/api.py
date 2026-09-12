"""Piloto API: preserva respostas, valida paginação e exporta metadados de busca.
A equivalência entre esta requisição e a interface web ainda exige conferência.
"""
import argparse
import csv
import io
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode
from .core import atomic, digest, now
from .network import Client

VERSION = '0.2.0'

def validate(data):
    obj = json.loads(data)
    if not isinstance(obj, dict) or obj.get('status') != 'OK':
        raise ValueError('API não retornou status OK.')
    total, records = obj.get('resultCount'), obj.get('records')
    if type(total) is not int or total < 0 or not isinstance(records, list):
        raise ValueError('resultCount/records inválidos.')
    ids = []
    for row in records:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
            raise ValueError('Registro sem ID válido.')
        if not isinstance(row.get('title'), str):
            raise ValueError('Registro sem título textual.')
        ids.append(row['id'])
    if len(ids) != len(set(ids)):
        raise ValueError('IDs repetidos na mesma página.')
    return obj

def strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return []

def normalize(row, provenance):
    authors = row.get('authors', {})
    primary = authors.get('primary', {}) if isinstance(authors, dict) else {}
    urls = row.get('urls', [])
    if not isinstance(urls, list):
        urls = []
    return dict(record_id=row['id'], title=row['title'],
                authors=list(primary) if isinstance(primary, dict) else strings(primary),
                formats=strings(row.get('formats', [])), languages=strings(row.get('languages', [])),
                subjects=strings(row.get('subjects', [])),
                urls=[u['url'] for u in urls if isinstance(u, dict) and isinstance(u.get('url'), str)],
                area_status='consulta_exploratoria_nao_validada', metadata_level='search',
                original_record=row, **provenance)

def build_url(config, page):
    params = [('lookfor', config['lookfor']), ('type', config['type']),
              ('page', str(page)), ('limit', str(config['limit']))]
    params += [('filter[]', value) for value in config['filters']]
    return config['endpoint'] + '?' + urlencode(params)

class Pilot:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.signature = digest(json.dumps(config, sort_keys=True, ensure_ascii=False).encode())
        self.db = sqlite3.connect(self.directory / 'api.sqlite')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, signature TEXT);
          CREATE TABLE IF NOT EXISTS pages (page INTEGER PRIMARY KEY, total INTEGER, sha TEXT, count INTEGER, url TEXT, ts TEXT);
          CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, page INTEGER, payload TEXT);
        ''')
        previous = self.db.execute('SELECT signature FROM settings WHERE id=1').fetchone()
        if previous and previous[0] != self.signature:
            self.db.close()
            raise ValueError('Consulta/configuração mudou. Use outro --data-dir para não misturar recortes.')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (self.signature,))

    def archive(self, data):
        sha = digest(data)
        path = self.directory / 'raw/api' / (sha + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if digest(path.read_bytes()) != sha:
                raise ValueError('Raw corrompido.')
        else:
            with path.open('xb') as f:
                f.write(data)
        return sha

    def run(self, client, target_pages):
        try:
            for page in range(1, target_pages + 1):
                if self.db.execute('SELECT 1 FROM pages WHERE page=?', (page,)).fetchone():
                    continue
                previous = self.db.execute('SELECT total,count FROM pages ORDER BY page DESC LIMIT 1').fetchone()
                collected = self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
                if previous and collected >= previous[0]:
                    break
                url = build_url(self.config, page)
                data = client.get(url)
                sha = self.archive(data)
                obj = validate(data)
                total, records = obj['resultCount'], obj['records']
                if previous and total != previous[0]:
                    raise ValueError('Total mudou durante o piloto; revisar catálogo/consulta antes de continuar.')
                if len(records) > self.config['limit']:
                    raise ValueError('API excedeu o limite solicitado; contrato de paginação não confirmado.')
                if not records and collected < total:
                    raise ValueError('Página vazia antes de atingir o total; paginação não confirmada.')
                if collected + len(records) > total:
                    raise ValueError('Quantidade recebida excede resultCount.')
                if len(records) < self.config['limit'] and collected + len(records) < total:
                    raise ValueError('Página curta antes do fim: limite/paginação não confirmados.')
                for row in records:
                    if self.db.execute('SELECT 1 FROM records WHERE id=?', (row['id'],)).fetchone():
                        raise ValueError('ID repetido entre páginas: paginação instável ou parâmetro ignorado.')
                    formats = strings(row.get('formats', []))
                    if not formats or not set(formats) <= {'masterThesis', 'doctoralThesis'}:
                        raise ValueError('Tipo de documento inesperado; conferir filtros da API.')
                ts = now()
                provenance = dict(source_url=url, harvest_ts=ts, sha256=sha,
                                  pipeline_version=VERSION, query_id=self.signature, page=page)
                with self.db:
                    for row in records:
                        normalized = normalize(row, provenance)
                        self.db.execute('INSERT INTO records VALUES(?,?,?)',
                                        (row['id'], page, json.dumps(normalized, ensure_ascii=False)))
                    self.db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?)',
                                    (page, total, sha, len(records), url, ts))
                print(f'Página {page}: {len(records)} registros; total informado: {total}')
            self.export()
        except Exception as exc:
            self.export(error=str(exc))
            raise

    def export(self, error=None):
        rows = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM records ORDER BY page,id')]
        pages = [dict(page=r[0], total=r[1], sha256=r[2], records=r[3], url=r[4], harvest_ts=r[5])
                 for r in self.db.execute('SELECT * FROM pages ORDER BY page')]
        atomic(self.directory / 'staging/api_metadados.jsonl', ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows))
        buffer = io.StringIO(newline='')
        writer = csv.writer(buffer, delimiter=';')
        writer.writerow(['id','titulo','autores','tipos','idiomas','assuntos','links','pagina'])
        for r in rows:
            values = [r['record_id'], r['title'], ' | '.join(r['authors']), ' | '.join(r['formats']),
                      ' | '.join(r['languages']), ' | '.join(r['subjects']), ' | '.join(r['urls']), str(r['page'])]
            # Visualização segura em planilhas; JSONL/Raw preservam os valores originais.
            writer.writerow(["'"+v if v.lstrip().startswith(('=', '+', '-', '@')) else v for v in values])
        atomic(self.directory / 'staging/conferencia.csv', '\ufeff' + buffer.getvalue())
        observed = pages[0]['total'] if pages else None
        expected = self.config.get('browser_reference_total')
        report = dict(version=VERSION, query=self.config, query_id=self.signature,
                      api_total=observed, browser_reference_total=expected,
                      same_total_as_screenshot=observed == expected if observed is not None else None,
                      pages_validated=len(pages), records_collected=len(rows),
                      records_without_links=sum(not r['urls'] for r in rows),
                      pagination_observed=len(pages) >= 2,
                      equivalent_to_browser='pendente_de_conferencia', area_validated=False,
                      pdfs_downloaded=0, error=error,
                      first_titles=[r['title'] for r in rows if r['page'] == 1][:5], pages=pages)
        atomic(self.directory / 'reports/api_pilot.json', json.dumps(report, ensure_ascii=False, indent=2))
        atomic(self.directory / 'raw/api_manifest.jsonl', ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in pages))
        return report

def main():
    parser = argparse.ArgumentParser(description='Piloto API BDTD: até 10 páginas, sem PDFs')
    parser.add_argument('--contact', required=True)
    parser.add_argument('--data-dir', type=Path, default=Path('data/api-piloto'))
    parser.add_argument('--config', type=Path, default=Path('conf/api-piloto.json'))
    parser.add_argument('--pages', type=int, default=2, help='Total de páginas desejado neste piloto, incluindo as já coletadas')
    args = parser.parse_args()
    if not 1 <= args.pages <= 10:
        parser.error('--pages deve estar entre 1 e 10 nesta fase de validação.')
    if '@' not in args.contact or any(c.isspace() for c in args.contact):
        parser.error('Informe um e-mail de contato válido.')
    pilot = None
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        pilot = Pilot(args.data_dir, config)
        client = Client(f'BDTD-Academic-Crawler/0.2 (contato: {args.contact})', log=args.data_dir / 'logs/http.jsonl')
        pilot.run(client, args.pages)
        print('Concluído. Relatório:', (args.data_dir / 'reports/api_pilot.json').resolve())
    except Exception as exc:
        parser.exit(1, f'Interrompido: {exc}\nConsulte data-dir/reports/api_pilot.json, se criado.\n')
    finally:
        if pilot:
            pilot.db.close()

if __name__ == '__main__':
    main()
