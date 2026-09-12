import argparse
import json
from pathlib import Path
from .core import Store, parse_oai
from .network import Client, harvest, spike

def main():
    parser = argparse.ArgumentParser(description='BDTD: início do pipeline, v0.1.0')
    parser.add_argument('--data-dir', default='data')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('demo', help='Importa apenas exemplos sintéticos; nunca acessa a rede')
    sub.add_parser('staging')
    sub.add_parser('status')
    imp = sub.add_parser('import-oai')
    imp.add_argument('file', type=Path)
    imp.add_argument('--source-url', required=True)
    for name in ('spike', 'harvest-oai'):
        child = sub.add_parser(name)
        child.add_argument('--contact', required=True, help='E-mail real do responsável pela coleta')
        child.add_argument('--config', type=Path, default=Path('conf/crawler.json'))
        if name == 'harvest-oai':
            child.add_argument('--endpoint', required=True)
            child.add_argument('--set', dest='set_spec', required=True, help='setSpec validado no spike')
            child.add_argument('--max-pages', type=int, default=2)
    args = parser.parse_args()
    directory = Path(args.data_dir)
    if args.cmd == 'demo':
        directory = directory / 'demo'
    store = Store(directory)
    try:
        if args.cmd == 'demo':
            sample = Path(__file__).resolve().parents[1] / 'examples' / 'synthetic.xml'
            store.archive(sample.read_bytes(), 'synthetic://demo', synthetic=True)
            store.staging()
        elif args.cmd == 'import-oai':
            data = args.file.read_bytes()
            parse_oai(data, 'ListRecords')
            store.archive(data, args.source_url)
            store.export()
        elif args.cmd == 'staging':
            store.staging()
        elif args.cmd in ('spike', 'harvest-oai'):
            if '@' not in args.contact or any(c.isspace() for c in args.contact):
                parser.error('Informe um e-mail de contato válido.')
            config = json.loads(args.config.read_text(encoding='utf-8'))
            client = Client(f'BDTD-Academic-Crawler/0.1 (contato: {args.contact})',
                            config['interval_seconds'], config['timeout_seconds'], directory / 'logs' / 'http.jsonl')
            if args.cmd == 'spike':
                print(json.dumps(spike(client, config, directory), ensure_ascii=False, indent=2))
            else:
                if args.max_pages < 1:
                    parser.error('--max-pages deve ser positivo.')
                print('Páginas coletadas:', harvest(store, client, args.endpoint, args.set_spec, args.max_pages))
        print('Registros estruturados:', store.export())
        print('Saídas:', directory.resolve())
    except Exception as exc:
        parser.exit(1, f'Interrompido: {exc}\nDados anteriores preservados. Consulte README.md.\n')
    finally:
        store.db.close()

if __name__ == '__main__':
    main()
