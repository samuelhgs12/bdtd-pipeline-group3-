"""Amostra reproduzível na janela de paginação realmente acessível da API BDTD."""
import argparse
import csv
import io
import json
import random
import sqlite3
from pathlib import Path

from .api import build_url, normalize, strings, validate
from .core import atomic, digest, now
from .network import Client

VERSION = '0.4.1'


class Sampler:
    def __init__(self, directory, config, seed=20260912, size=50, frame_size=1000):
        if not 1 <= size <= 50:
            raise ValueError('A amostra deve conter entre 1 e 50 registros.')
        if frame_size < size:
            raise ValueError('A janela de amostragem deve comportar a amostra.')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.seed = seed
        self.size = size
        self.frame_size = frame_size
        contract = dict(config=config, seed=seed, size=size, limit=1,
                        frame_size=frame_size, version=VERSION)
        self.signature = digest(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode())
        self.db = sqlite3.connect(self.directory / 'sample.sqlite')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, signature TEXT, total INTEGER);
          CREATE TABLE IF NOT EXISTS pages (page INTEGER PRIMARY KEY, total INTEGER, sha TEXT, count INTEGER, url TEXT, ts TEXT, payload TEXT);
          CREATE TABLE IF NOT EXISTS selections (sample_order INTEGER PRIMARY KEY, rank INTEGER UNIQUE, record_id TEXT UNIQUE, payload TEXT);
        ''')
        previous = self.db.execute('SELECT signature FROM settings WHERE id=1').fetchone()
        if previous and previous[0] != self.signature:
            self.db.close()
            raise ValueError('Consulta, seed, tamanho ou janela mudou. Use outro --data-dir.')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?,NULL)', (1, self.signature))

    def archive(self, data):
        sha = digest(data)
        path = self.directory / 'raw/api' / f'{sha}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and digest(path.read_bytes()) != sha:
            raise ValueError('Raw da amostra corrompido.')
        if not path.exists():
            path.write_bytes(data)
        return sha

    def page(self, client, page):
        saved = self.db.execute('SELECT payload FROM pages WHERE page=?', (page,)).fetchone()
        if saved:
            return json.loads(saved[0]), True
        url = build_url(self.config, page, limit=1)
        data = client.get(url)
        sha = self.archive(data)
        obj = validate(data)
        ts = now()
        payload = json.dumps(obj, ensure_ascii=False)
        with self.db:
            self.db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?)',
                            (page, obj['resultCount'], sha, len(obj['records']), url, ts, payload))
        return obj, False

    def run(self, client):
        first, _ = self.page(client, 1)
        total = first['resultCount']
        if total < self.size:
            raise ValueError('População menor que a amostra solicitada.')
        if self.frame_size > total:
            raise ValueError('A janela de amostragem excede o total informado pela API.')
        prior_total = self.db.execute('SELECT total FROM settings WHERE id=1').fetchone()[0]
        if prior_total is not None and prior_total != total:
            raise ValueError('Total mudou desde o início da amostragem.')
        with self.db:
            self.db.execute('UPDATE settings SET total=? WHERE id=1', (total,))

        frame_last, _ = self.page(client, self.frame_size)
        if frame_last['resultCount'] != total or len(frame_last['records']) != 1:
            raise ValueError('A última posição da janela não pôde ser alcançada.')
        frame_after, _ = self.page(client, self.frame_size + 1)
        if frame_after['resultCount'] != total:
            raise ValueError('Total mudou ao validar o limite da janela.')
        if total == self.frame_size:
            if frame_after['records']:
                raise ValueError('A API retornou dados depois do total informado.')
            boundary_status = 'vazia_no_fim_da_populacao'
        else:
            if (len(frame_after['records']) != 1 or
                    frame_after['records'][0]['id'] != frame_last['records'][0]['id']):
                raise ValueError('A posição após a janela ainda avança; revise --frame-size.')
            boundary_status = 'repete_ultima_posicao_da_janela'

        ranks = random.Random(self.seed).sample(range(1, self.frame_size + 1), self.size)
        for sample_order, rank in enumerate(ranks, 1):
            if self.db.execute('SELECT 1 FROM selections WHERE sample_order=?',
                               (sample_order,)).fetchone():
                continue
            obj, _ = self.page(client, rank)
            if obj['resultCount'] != total:
                raise ValueError(f'Total mudou ao consultar a posição {rank}.')
            if len(obj['records']) != 1:
                raise ValueError(f'Posição {rank} não retornou exatamente um registro.')
            row = obj['records'][0]
            if self.db.execute('SELECT 1 FROM selections WHERE record_id=?', (row['id'],)).fetchone():
                raise ValueError(f'ID repetido em posições sorteadas: {row["id"]}.')
            formats = strings(row.get('formats', []))
            if not formats or not set(formats) <= {'masterThesis', 'doctoralThesis'}:
                raise ValueError(f'Tipo inesperado na posição {rank}.')
            page_data = self.db.execute('SELECT sha,url,ts FROM pages WHERE page=?', (rank,)).fetchone()
            provenance = dict(source_url=page_data[1], harvest_ts=page_data[2], sha256=page_data[0],
                              pipeline_version=VERSION, query_id=self.signature, page=rank,
                              sample_rank=rank, sample_order=sample_order,
                              sampling_seed=self.seed, sampling_frame_total=self.frame_size,
                              api_result_total=total)
            selected = normalize(row, provenance)
            with self.db:
                self.db.execute('INSERT INTO selections VALUES(?,?,?,?)',
                                (sample_order, rank, row['id'],
                                 json.dumps(selected, ensure_ascii=False)))
            self.export(total, boundary_status)
            print(f'Amostra {sample_order}/{self.size}: posição {rank}', flush=True)
        return self.export(total, boundary_status)

    def export(self, total=None, boundary_status=None):
        settings_total = self.db.execute('SELECT total FROM settings WHERE id=1').fetchone()[0]
        total = total if total is not None else settings_total
        selected = [json.loads(row[0]) for row in
                    self.db.execute('SELECT payload FROM selections ORDER BY sample_order')]
        pages = [dict(page=row[0], total=row[1], sha256=row[2], records=row[3],
                      url=row[4], harvest_ts=row[5])
                 for row in self.db.execute('SELECT page,total,sha,count,url,ts FROM pages ORDER BY page')]
        complete = len(selected) == self.size
        if complete:
            atomic(self.directory / 'selected_50.jsonl',
                   ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in selected))
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer, delimiter=';', lineterminator='\n')
            writer.writerow(['ordem_amostra', 'posicao_resultados', 'record_id', 'titulo',
                             'tipos', 'assuntos', 'urls'])
            for row in selected:
                values = [str(row['sample_order']), str(row['sample_rank']), row['record_id'],
                          row['title'], ' | '.join(row['formats']), ' | '.join(row['subjects']),
                          ' | '.join(row['urls'])]
                writer.writerow(["'" + value if value.lstrip().startswith(('=', '+', '-', '@'))
                                 else value for value in values])
            atomic(self.directory / 'selected_50.csv', '\ufeff' + buffer.getvalue())

        bins = [0] * 10
        for row in selected:
            bins[min(9, (row['sample_rank'] - 1) * 10 // self.frame_size)] += 1
        last = self.db.execute('SELECT payload FROM pages WHERE page=?',
                               (self.frame_size,)).fetchone()
        after = self.db.execute('SELECT payload FROM pages WHERE page=?',
                                (self.frame_size + 1,)).fetchone()
        last_obj = json.loads(last[0]) if last else None
        after_obj = json.loads(after[0]) if after else None
        repeated = bool(last_obj and after_obj and last_obj['records'] and after_obj['records'] and
                        last_obj['records'][0]['id'] == after_obj['records'][0]['id'])
        if boundary_status is None:
            if repeated:
                boundary_status = 'repete_ultima_posicao_da_janela'
            elif after_obj and not after_obj['records']:
                boundary_status = 'vazia_no_fim_da_populacao'
            else:
                boundary_status = 'nao_testado'
        report = dict(
            version=VERSION, query=self.config, query_id=self.signature, seed=self.seed,
            requested_size=self.size, selected_records=len(selected), complete=complete,
            api_total=total, accessible_frame_size=self.frame_size,
            inaccessible_reported_positions=max(0, (total or 0) - self.frame_size),
            limit_per_request=1,
            sampling_method='random.sample sobre posições 1..accessible_frame_size',
            population_random_sample=False,
            accessible_frame_boundary_reachable=bool(last_obj and len(last_obj['records']) == 1),
            position_after_frame=boundary_status,
            position_after_frame_repeats_boundary=repeated,
            position_deciles_within_accessible_frame=bins,
            min_position=min((row['sample_rank'] for row in selected), default=None),
            max_position=max((row['sample_rank'] for row in selected), default=None),
            unique_record_ids=len({row['record_id'] for row in selected}),
            pages_requested=len(pages),
            limitations=[
                'A API informa mais resultados do que sua janela de paginação permite alcançar; esta amostra representa apenas as primeiras posições acessíveis.',
                'A ordenação padrão da API não é um snapshot imutável; atualizações do índice podem alterar posições em outra data.',
                'A pertinência temática ainda depende de revisão humana.'
            ], generated_at=now())
        atomic(self.directory / 'sample_report.json',
               json.dumps(report, ensure_ascii=False, indent=2))
        atomic(self.directory / 'raw/api_manifest.jsonl',
               ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in pages))
        return report


def main():
    parser = argparse.ArgumentParser(description='Amostra reproduzível de até 50 registros da API BDTD')
    parser.add_argument('--contact', required=True)
    parser.add_argument('--config', type=Path,
                        default=Path('conf/api-validacao-linguagem-comunicacao.json'))
    parser.add_argument('--data-dir', type=Path,
                        default=Path('data/validacao-linguagem-comunicacao/sample-accessible'))
    parser.add_argument('--seed', type=int, default=20260912)
    parser.add_argument('--size', type=int, default=50)
    parser.add_argument('--frame-size', type=int, default=1000,
                        help='Janela alcançável confirmada; use outro data-dir se mudar')
    args = parser.parse_args()
    if '@' not in args.contact or any(char.isspace() for char in args.contact):
        parser.error('Informe um e-mail válido.')
    sampler = None
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        sampler = Sampler(args.data_dir, config, args.seed, args.size, args.frame_size)
        client = Client(f'BDTD-Academic-Crawler/{VERSION} (contato: {args.contact})',
                        log=args.data_dir / 'logs/http.jsonl')
        report = sampler.run(client)
        print('Selecionados:', report['selected_records'])
        print('Lista:', (args.data_dir / 'selected_50.jsonl').resolve())
    except KeyboardInterrupt:
        if sampler:
            sampler.export()
        parser.exit(130, 'Interrompido; páginas e seleções concluídas foram preservadas.\n')
    except Exception as exc:
        if sampler:
            sampler.export()
        parser.exit(1, f'Interrompido: {exc}\n')
    finally:
        if sampler:
            sampler.db.close()


if __name__ == '__main__':
    main()
