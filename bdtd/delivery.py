"""Prepara e verifica a entrega final sem copiar ou remover PDFs."""
import argparse
import hashlib
import json
from pathlib import Path

from .batches import jsonl,sha256
from .core import atomic,now
from .pdfs import obvious_site_document


def tree_hash(files):
    text=''.join(f'{item["sha256"]}  {item["destination_path"]}\n'
                 for item in sorted(files,key=lambda item:item['destination_path']))
    return hashlib.sha256(text.encode()).hexdigest()


def entry(project,path,destination):
    path=Path(path)
    return dict(source_path=path.relative_to(project).as_posix(),destination_path=destination,
                bytes=path.stat().st_size,sha256=sha256(path))


def prepare(project,data_dir):
    project=Path(project).resolve(); data=Path(data_dir).resolve()
    plan=json.loads((data/'reports/batch_transfer_plan.json').read_text(encoding='utf-8'))
    results=jsonl(data/'pdfs/raw/manifest_pdf.jsonl')
    metadata={row['record_id']:row for row in jsonl(data/'metadata/records.jsonl')}
    packaged={row['sha256'] for batch in plan['batches'] for row in batch['pdf_associations']}
    candidate={item['sha256']:item for result in results for item in result.get('files',[])
               if not obvious_site_document(item.get('url',''))}
    remaining=sorted(set(candidate)-packaged)
    associations=[dict(record_id=result['record_id'],sha256=item['sha256'],
                       source_url=item.get('url',''),bytes=item['bytes'],
                       pdf_path=f'pdfs/{item["sha256"]}.pdf')
                  for result in results for item in result.get('files',[])
                  if item['sha256'] in remaining]
    record_ids=sorted({row['record_id'] for row in associations})
    support=data/'delivery/residual-batch-0011'; support.mkdir(parents=True,exist_ok=True)
    atomic(support/'manifest.jsonl',''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in associations))
    atomic(support/'metadata.jsonl',''.join(json.dumps(metadata[row],ensure_ascii=False)+'\n'
                                            for row in record_ids if row in metadata))
    by_result={row['record_id']:row for row in results}
    atomic(support/'download_results.jsonl',''.join(json.dumps(by_result[row],ensure_ascii=False)+'\n'
                                                    for row in record_ids))
    quality=[dict(record_id=row['record_id'],title=row.get('title'),
                  metadata_quality_flags=row.get('metadata_quality_flags',[]))
             for row in metadata.values() if row.get('metadata_quality_flags')]
    atomic(data/'reports/metadata_quality_flags.jsonl',''.join(json.dumps(row,ensure_ascii=False)+'\n'
                                                               for row in quality))
    institutional=[dict(record_id=result['record_id'],**item) for result in results
                   for item in result.get('files',[]) if obvious_site_document(item.get('url',''))]
    atomic(data/'reports/institutional_documents.jsonl',''.join(json.dumps(row,ensure_ascii=False)+'\n'
                                                                for row in institutional))
    extras=[]
    for result in results:
        files=[item for item in result.get('files',[]) if not obvious_site_document(item.get('url',''))]
        if len(files)>1:
            extras.append(dict(record_id=result['record_id'],candidate_files=files,
                               extra_candidate_count=len(files)-1))
    atomic(data/'reports/possible_extra_candidates.jsonl',''.join(json.dumps(row,ensure_ascii=False)+'\n'
                                                                  for row in extras))

    groups=[]
    data_relative=data.relative_to(project).as_posix()
    for batch in plan['batches']:
        files=[dict(source_path=data_relative+'/'+item['path'],
                    destination_path=item['path'],bytes=item['bytes'],sha256=item['sha256'])
               for item in batch['files']]
        groups.append(dict(id=batch['batch_id'],kind='closed_batch',pdf_count=batch['pdf_count'],
                           bytes=sum(item['bytes'] for item in files),files=files,
                           tree_sha256=tree_hash(files)))
    residual_files=[]
    for file_sha in remaining:
        item=candidate[file_sha]; source=data/'pdfs'/item['path']
        if not source.exists() or sha256(source)!=file_sha:
            raise ValueError('PDF residual ausente ou corrompido: '+str(source))
        residual_files.append(entry(project,source,f'batches/batch-0011/pdfs/{file_sha}.pdf'))
    for name in ('manifest.jsonl','metadata.jsonl','download_results.jsonl'):
        residual_files.append(entry(project,support/name,f'batches/batch-0011/{name}'))
    groups.append(dict(id='batch-0011',kind='residual_transfer_set',pdf_count=len(remaining),
                       bytes=sum(item['bytes'] for item in residual_files),files=residual_files,
                       tree_sha256=tree_hash(residual_files)))
    support_sources=[(project/'README.md','delivery/README.md'),
        (data/'reports/collection.json','delivery/collection.json'),
        (data/'metadata/rejections.jsonl','delivery/rejections.jsonl'),
        (data/'reports/metadata_quality_flags.jsonl','delivery/metadata_quality_flags.jsonl'),
        (data/'reports/possible_extra_candidates.jsonl','delivery/possible_extra_candidates.jsonl'),
        (data/'reports/institutional_documents.jsonl','delivery/institutional_documents.jsonl'),
        (data/'pdfs/reports/pdf_pilot.json','delivery/pdf_pilot.json')]
    support_files=[entry(project,source,destination) for source,destination in support_sources]
    groups.append(dict(id='delivery-metadata',kind='documentation',pdf_count=0,
                       bytes=sum(item['bytes'] for item in support_files),files=support_files,
                       tree_sha256=tree_hash(support_files)))
    inventory=dict(version='0.4.0',generated_at=now(),source_root=project.as_posix(),
        destination_layout_root='.',candidate_unique_pdfs=len(candidate),
        closed_batch_pdfs=len(packaged),residual_pdfs=len(remaining),
        reconciled=(len(packaged)+len(remaining)==len(candidate)),
        possible_extra_candidate_files=sum(row['extra_candidate_count'] for row in extras),
        institutional_documents=len(institutional),quality_flagged_records=len(quality),
        rejection_records=len(jsonl(data/'metadata/rejections.jsonl')),groups=groups)
    output=data/'reports/final_delivery_inventory.json'
    atomic(output,json.dumps(inventory,ensure_ascii=False,indent=2))
    atomic(Path(str(output)+'.sha256'),sha256(output)+'  '+output.name+'\n')
    return output,inventory


def verify(destination,inventory_path):
    destination=Path(destination); inventory=json.loads(Path(inventory_path).read_text(encoding='utf-8'))
    checked=0
    for group in inventory['groups']:
        observed=[]
        for item in group['files']:
            path=destination/item['destination_path']
            if not path.exists() or path.stat().st_size!=item['bytes'] or sha256(path)!=item['sha256']:
                raise ValueError('Cópia ausente ou divergente: '+str(path))
            observed.append(item); checked+=1
        if tree_hash(observed)!=group['tree_sha256']:
            raise ValueError('Hash da árvore diverge: '+group['id'])
    return checked


def main():
    parser=argparse.ArgumentParser(description='Prepara ou verifica a entrega final da coleta')
    parser.add_argument('--data-dir',type=Path,default=Path('data/coleta-5000'))
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--verify-destination',type=Path)
    parser.add_argument('--inventory',type=Path)
    args=parser.parse_args()
    if args.prepare:
        output,report=prepare(Path.cwd(),args.data_dir)
        print('Inventário:',output.resolve(),'PDFs:',report['candidate_unique_pdfs'])
    elif args.verify_destination:
        inventory=args.inventory or args.data_dir/'reports/final_delivery_inventory.json'
        print('Arquivos verificados:',verify(args.verify_destination,inventory))
    else: parser.error('Use --prepare ou --verify-destination.')


if __name__=='__main__': main()
