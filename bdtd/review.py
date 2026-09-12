"""Une amostra, downloads e triagem preliminar em uma tabela de revisão humana."""
import argparse
import collections
import csv
import html
import io
import json
import re
from pathlib import Path

from .core import atomic, digest, now
from .pdfs import obvious_site_document

VERSION = '0.4.1'
CLASSES = {'pertinente', 'não pertinente', 'duvidoso'}
COMPLETED = {'pdf_obtido', 'pdf_duplicado', 'links_localizados'}


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8-sig').splitlines()
            if line.strip()]


def clean_text(value):
    value = re.sub(r'<[^>]+>', ' ', html.unescape(value or ''))
    return re.sub(r'\s+', ' ', value).strip()


def abstract_for(row, metadata):
    values = list(row.get('abstracts', []))
    for item in metadata:
        fields = item.get('fields', {})
        values.extend(fields.get('dcterms.abstract', []))
        descriptions = [clean_text(value) for value in fields.get('dc.description', [])]
        values.extend(value for value in descriptions
                      if len(value) > 250 and not value.lower().startswith(('dissertação ', 'tese ')))
    cleaned = []
    for value in values:
        value = clean_text(value)
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned[0] if cleaned else ''


def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def build_review(sample_path, manifest_path, metadata_path, triage_path, output_dir):
    sample_bytes = Path(sample_path).read_bytes()
    sample = load_jsonl(sample_path)
    results = {row['record_id']: row for row in load_jsonl(manifest_path)}
    metadata = collections.defaultdict(list)
    if Path(metadata_path).exists():
        for row in load_jsonl(metadata_path):
            metadata[row['record_id']].append(row)
    triage_doc = json.loads(Path(triage_path).read_text(encoding='utf-8'))
    if triage_doc.get('sample_sha256') != digest(sample_bytes):
        raise ValueError('A triagem não corresponde ao snapshot da amostra.')
    triage = {row['record_id']: row for row in triage_doc['classifications']}
    sample_ids = [row['record_id'] for row in sample]
    if len(sample_ids) != 50 or len(set(sample_ids)) != 50:
        raise ValueError('A revisão exige exatamente 50 registros distintos.')
    if set(results) != set(sample_ids):
        raise ValueError('O manifesto de downloads não cobre exatamente a amostra.')
    if set(triage) != set(sample_ids):
        raise ValueError('A triagem preliminar não cobre exatamente a amostra.')

    rows = []
    for source in sample:
        record_id = source['record_id']
        result = results[record_id]
        preliminary = triage[record_id]
        if preliminary['classification'] not in CLASSES:
            raise ValueError(f'Classificação inválida: {record_id}.')
        files = result.get('files', [])
        site_files = [item for item in files if obvious_site_document(item.get('url', ''))]
        candidate_files = [item for item in files if item not in site_files]
        warnings = list(dict.fromkeys(attempt['status'] for attempt in result.get('attempts', [])
                                     if attempt['status'] not in COMPLETED))
        outcome = f'{result["status"]}; {len(files)} PDF(s) obtido(s)'
        if site_files:
            outcome += f'; {len(site_files)} documento(s) institucional(is) do site'
        if warnings:
            outcome += '; ocorrências=' + ', '.join(warnings)
        rows.append(dict(
            sample_order=source['sample_order'], sample_position=source['sample_rank'],
            record_id=record_id, title=clean_text(source['title']),
            abstract=abstract_for(source, metadata[record_id]),
            subjects=' | '.join(clean_text(value) for value in source.get('subjects', [])),
            origin_url=' | '.join(clean_text(value) for value in source.get('urls', [])),
            download_result=outcome, work_has_pdf=bool(candidate_files),
            pdf_files_obtained=len(files), candidate_work_pdf_files=len(candidate_files),
            site_document_pdf_files=len(site_files),
            possible_extra_files=max(0, len(candidate_files) - 1),
            preliminary_classification=preliminary['classification'],
            short_justification=clean_text(preliminary['justification']), final_decision=''))

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic(output_dir / 'review_50.jsonl',
           ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    fields = ['sample_order', 'sample_position', 'record_id', 'title', 'abstract', 'subjects',
              'origin_url', 'download_result', 'work_has_pdf', 'pdf_files_obtained',
              'candidate_work_pdf_files', 'site_document_pdf_files', 'possible_extra_files',
              'preliminary_classification', 'short_justification',
              'final_decision']
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=fields, delimiter=';', lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: safe_cell(value) for key, value in row.items()})
    atomic(output_dir / 'review_50.csv', '\ufeff' + buffer.getvalue())

    all_files = [item for result in results.values() for item in result.get('files', [])]
    site_files = [item for item in all_files if obvious_site_document(item.get('url', ''))]
    candidate_files = [item for item in all_files if item not in site_files]
    hashes = {item['sha256'] for item in all_files}
    relevance = collections.Counter(row['preliminary_classification'] for row in rows)
    download = collections.Counter(results[record_id]['status'] for record_id in sample_ids)
    works_with_pdf = sum(row['work_has_pdf'] for row in rows)
    report = dict(
        version=VERSION, generated_at=now(), sample_sha256=digest(sample_bytes),
        selected_works=len(rows),
        download=dict(works_with_at_least_one_pdf=works_with_pdf,
                      success_rate=works_with_pdf / len(rows),
                      unique_pdf_files_obtained=len(hashes), pdf_file_references=len(all_files),
                      candidate_work_pdf_files=len({item['sha256'] for item in candidate_files}),
                      site_document_pdf_files=len({item['sha256'] for item in site_files}),
                      works_with_multiple_candidate_files=sum(row['candidate_work_pdf_files'] > 1 for row in rows),
                      possible_extra_files=sum(row['possible_extra_files'] for row in rows),
                      document_roles_verified=0, statuses=dict(download)),
        preliminary_relevance=dict(counts=dict(relevance), human_validation=False,
                                   excluded_records=0),
        metadata=dict(records_with_abstract=sum(bool(row['abstract']) for row in rows),
                      records_without_abstract=sum(not row['abstract'] for row in rows)),
        limitations=[
            'A classificação é preliminar e não substitui a decisão humana.',
            'Resumo foi incluído apenas quando presente na API ou nos metadados HTML obtidos.',
            'Arquivos candidatos além do primeiro são possíveis anexos; seu papel documental não foi verificado.'
        ])
    atomic(output_dir / 'review_report.json', json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description='Gera tabela de revisão dos 50 trabalhos')
    parser.add_argument('--sample', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--metadata-html', type=Path, required=True)
    parser.add_argument('--triage', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_review(args.sample, args.manifest, args.metadata_html,
                              args.triage, args.output_dir)
        print('Tabela:', (args.output_dir / 'review_50.csv').resolve())
        print('Relatório:', (args.output_dir / 'review_report.json').resolve())
        print('Trabalhos com PDF:', report['download']['works_with_at_least_one_pdf'])
    except Exception as exc:
        parser.exit(1, f'Interrompido: {exc}\n')


if __name__ == '__main__':
    main()
