"""Fecha lotes imutáveis de PDFs e metadados, sem remover os objetos Raw."""
import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
from pathlib import Path

from .core import atomic, now
from .pdfs import obvious_site_document

VERSION='0.4.0'


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def jsonl(path):
    path=Path(path)
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


class BatchBuilder:
    def __init__(self,directory,pdf_directory,reserve_bytes=10*1024**3,target_bytes=1024**3):
        self.directory=Path(directory)
        self.pdf_directory=Path(pdf_directory)
        self.reserve_bytes=reserve_bytes
        self.target_bytes=target_bytes
        self.directory.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.directory/'batches.sqlite')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS batches (number INTEGER PRIMARY KEY,status TEXT,bytes INTEGER,
            files INTEGER,manifest_sha TEXT,closed_at TEXT,path TEXT UNIQUE);
          CREATE TABLE IF NOT EXISTS files (sha TEXT PRIMARY KEY,batch_number INTEGER,path TEXT,bytes INTEGER);
          CREATE TABLE IF NOT EXISTS associations (record_id TEXT,sha TEXT,batch_number INTEGER,
            included_in_batch INTEGER,discovered_at TEXT,PRIMARY KEY(record_id,sha));
        ''')
        columns={row[1] for row in self.db.execute('PRAGMA table_info(batches)')}
        for name,kind in (('archive_status','TEXT'),('external_location','TEXT'),
                          ('transfer_verified_at','TEXT'),('transfer_manifest_sha','TEXT')):
            if name not in columns:
                with self.db: self.db.execute(f'ALTER TABLE batches ADD COLUMN {name} {kind}')
        self.recover()

    def recover(self):
        """Preserva construções incompletas e adota lotes fechados após interrupção."""
        interrupted=self.directory/'interrupted'
        for building in sorted(self.directory.glob('.building-*')):
            suffix=building.name.removeprefix('.building-')
            final=self.directory/f'batch-{suffix}'
            try:
                complete=(building/'CLOSED.json').exists() and self.verify(building)
            except Exception:
                complete=False
            if complete and not final.exists():
                os.replace(building,final)
            else:
                interrupted.mkdir(parents=True,exist_ok=True)
                stamp=now().replace(':','').replace('-','')
                preserved=interrupted/(building.name+'.'+stamp)
                counter=1
                while preserved.exists():
                    preserved=interrupted/(building.name+f'.{counter}')
                    counter+=1
                os.replace(building,preserved)
        for final in sorted(self.directory.glob('batch-*')):
            if not final.is_dir(): continue
            number=int(final.name.removeprefix('batch-'))
            if not self.db.execute('SELECT 1 FROM batches WHERE number=?',(number,)).fetchone():
                self.adopt(final,number)

    def adopt(self,path,number):
        self.verify(path)
        closed=json.loads((path/'CLOSED.json').read_text(encoding='utf-8'))
        manifest=jsonl(path/'manifest.jsonl')
        unique={row['sha256']:row for row in manifest}
        with self.db:
            self.db.execute('''INSERT INTO batches
              (number,status,bytes,files,manifest_sha,closed_at,path) VALUES(?,?,?,?,?,?,?)''',
                            (number,'closed',closed['bytes'],closed['candidate_pdf_files'],
                             closed['package_checksum_sha256'],closed['closed_at'],
                             path.relative_to(self.directory).as_posix()))
            for file_sha,row in unique.items():
                self.db.execute('INSERT OR IGNORE INTO files VALUES(?,?,?,?)',
                                (file_sha,number,row['pdf_path'],row['bytes']))
            for row in manifest:
                self.db.execute('INSERT OR IGNORE INTO associations VALUES(?,?,?,?,?)',
                                (row['record_id'],row['sha256'],number,1,now()))

    def inventory(self,manifest_path):
        results=jsonl(manifest_path)
        files={}
        associations=[]
        packaged={row[0]:row[1] for row in self.db.execute('SELECT sha,batch_number FROM files')}
        for result in results:
            for item in result.get('files',[]):
                if obvious_site_document(item.get('url','')): continue
                associations.append((result['record_id'],item['sha256'],item.get('url','')))
                if item['sha256'] in packaged:
                    continue
                path=self.pdf_directory/item['path']
                if not path.exists() or sha256(path)!=item['sha256']:
                    raise ValueError('PDF ausente ou corrompido e ainda não empacotado: '+str(path))
                files.setdefault(item['sha256'],dict(item,absolute_path=path))
        with self.db:
            for record_id,file_sha,_ in associations:
                if file_sha in packaged:
                    self.db.execute('INSERT OR IGNORE INTO associations VALUES(?,?,?,?,?)',
                                    (record_id,file_sha,packaged[file_sha],0,now()))
        self.export_updates(associations)
        return files,associations,results

    def transfer_plan(self,output_path):
        batches=[]
        for number,path_value in self.db.execute(
                "SELECT number,path FROM batches WHERE status='closed' ORDER BY number"):
            batch=self.directory/path_value
            self.verify(batch)
            manifest=jsonl(batch/'manifest.jsonl'); files=[]
            for path in sorted(item for item in batch.rglob('*') if item.is_file()):
                files.append(dict(path=path.relative_to(self.directory.parent).as_posix(),
                                  bytes=path.stat().st_size,sha256=sha256(path)))
            tree_text=''.join(f'{item["sha256"]}  {item["path"]}\n' for item in files)
            pdf_rows={row['sha256']:row for row in manifest}
            closed=json.loads((batch/'CLOSED.json').read_text(encoding='utf-8'))
            expected_pdf_paths={row['pdf_path'] for row in pdf_rows.values()}
            actual_pdf_paths={path.relative_to(batch).as_posix() for path in (batch/'pdfs').glob('*.pdf')}
            if expected_pdf_paths!=actual_pdf_paths or len(pdf_rows)!=closed['candidate_pdf_files']:
                raise ValueError('Conjunto de PDFs do lote diverge do manifesto/CLOSED.json: '+str(batch))
            record_ids={row['record_id'] for row in manifest}
            metadata_ids={row['record_id'] for row in jsonl(batch/'metadata/records.jsonl')}
            result_ids={row['record_id'] for row in jsonl(batch/'results/download_results.jsonl')}
            if not record_ids<=metadata_ids or not record_ids<=result_ids:
                raise ValueError('Associação de registro ausente nos metadados/resultados: '+str(batch))
            raw_duplicates=0; hardlinks=0
            for file_sha,row in pdf_rows.items():
                packaged=batch/row['pdf_path']
                if (not packaged.exists() or packaged.stat().st_size!=row['bytes'] or
                        sha256(packaged)!=file_sha):
                    raise ValueError('PDF do lote diverge do manifesto: '+str(packaged))
                with packaged.open('rb') as stream:
                    header=stream.read(5)
                    stream.seek(max(0,packaged.stat().st_size-4096)); tail=stream.read()
                if header!=b'%PDF-' or b'%%EOF' not in tail:
                    raise ValueError('Arquivo do lote não tem estrutura PDF esperada: '+str(packaged))
                raw=self.pdf_directory/'raw/pdf'/file_sha[:2]/f'{file_sha}.pdf'
                if raw.exists() and sha256(raw)==file_sha:
                    raw_duplicates+=1
                    try: hardlinks+=int(os.path.samefile(packaged,raw))
                    except OSError: pass
            batches.append(dict(batch_number=number,batch_id=f'batch-{number:04d}',
                path=batch.relative_to(self.directory.parent).as_posix(),format='directory',
                bytes=sum(item['bytes'] for item in files),file_count=len(files),
                pdf_count=len(pdf_rows),pdf_format='PDF',
                pdf_bytes=sum(row['bytes'] for row in pdf_rows.values()),
                package_checksum_sha256=closed['package_checksum_sha256'],
                transfer_tree_sha256=hashlib.sha256(tree_text.encode()).hexdigest(),files=files,
                pdf_associations=[{key:row.get(key) for key in
                    ('record_id','pdf_path','source_url','sha256','bytes')} for row in manifest],
                raw_duplicate_pdfs=raw_duplicates,raw_hardlinks=hardlinks))
        report=dict(version=VERSION,generated_at=now(),root=self.directory.parent.as_posix(),
                    batches=batches,total_batches=len(batches),
                    total_bytes=sum(item['bytes'] for item in batches),
                    total_pdfs=sum(item['pdf_count'] for item in batches),
                    unique_pdf_bytes=sum(item['pdf_bytes'] for item in batches),
                    simulated_releasable_bytes=dict(
                        remove_batch_directories_only=sum(item['bytes']-item['pdf_bytes'] for item in batches),
                        remove_packaged_raw_links_only=0,
                        remove_batches_and_corresponding_raw_after_verified_transfer=
                            sum(item['bytes'] for item in batches)))
        atomic(output_path,json.dumps(report,ensure_ascii=False,indent=2))
        atomic(Path(str(output_path)+'.sha256'),sha256(output_path)+'  '+Path(output_path).name+'\n')
        buffer=io.StringIO(newline=''); writer=csv.writer(buffer,delimiter=';',lineterminator='\n')
        writer.writerow(['batch_id','path','format','bytes','pdf_count','pdf_format','pdf_bytes',
                         'transfer_tree_sha256','package_checksum_sha256','raw_hardlinks'])
        for item in batches:
            writer.writerow([item[key] for key in ('batch_id','path','format','bytes','pdf_count',
                'pdf_format','pdf_bytes','transfer_tree_sha256','package_checksum_sha256','raw_hardlinks')])
        atomic(Path(output_path).with_suffix('.csv'),'\ufeff'+buffer.getvalue())
        return report

    def mark_transferred(self,number,external_location,plan_path):
        plan=json.loads(Path(plan_path).read_text(encoding='utf-8'))
        item=next((row for row in plan.get('batches',[]) if row.get('batch_number')==number),None)
        row=self.db.execute('SELECT path FROM batches WHERE number=? AND status=?',(number,'closed')).fetchone()
        if not item or not row: raise ValueError('Lote fechado não consta no plano de transferência.')
        batch=self.directory/row[0]; self.verify(batch)
        current_files=[dict(path=path.relative_to(self.directory.parent).as_posix(),
                            bytes=path.stat().st_size,sha256=sha256(path))
                       for path in sorted(value for value in batch.rglob('*') if value.is_file())]
        tree=''.join(f'{value["sha256"]}  {value["path"]}\n' for value in current_files)
        if hashlib.sha256(tree.encode()).hexdigest()!=item['transfer_tree_sha256']:
            raise ValueError('Lote local diverge do plano de transferência.')
        plan_sha=sha256(plan_path)
        with self.db:
            self.db.execute('''UPDATE batches SET archive_status='transferred_verified',
              external_location=?,transfer_verified_at=?,transfer_manifest_sha=? WHERE number=?''',
              (external_location,now(),plan_sha,number))

    def externally_archived_hashes(self):
        return {row[0] for row in self.db.execute('''SELECT files.sha FROM files JOIN batches
          ON batches.number=files.batch_number WHERE batches.archive_status='transferred_verified' ''')}

    def export_updates(self,associations):
        urls={(record_id,file_sha):url for record_id,file_sha,url in associations}
        rows=[dict(record_id=row[0],sha256=row[1],batch_number=row[2],
                   source_url=urls.get((row[0],row[1]),''),discovered_at=row[4])
              for row in self.db.execute('''SELECT record_id,sha,batch_number,included_in_batch,discovered_at
                                            FROM associations WHERE included_in_batch=0
                                            ORDER BY batch_number,record_id,sha''')]
        atomic(self.directory/'association_updates.jsonl',
               ''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))

    def close_ready(self,manifest_path,metadata_path,force=False):
        files,associations,results=self.inventory(manifest_path)
        packaged={row[0] for row in self.db.execute('SELECT sha FROM files')}
        pending=[item for key,item in sorted(files.items()) if key not in packaged]
        total=sum(item['bytes'] for item in pending)
        if not pending or (not force and total<self.target_bytes): return None
        selected=[]; selected_bytes=0
        for item in pending:
            if selected and selected_bytes>=self.target_bytes: break
            selected.append(item); selected_bytes+=item['bytes']
        free=shutil.disk_usage(self.directory).free
        if free<=self.reserve_bytes:
            raise RuntimeError('Reserva mínima de disco atingida antes do empacotamento.')
        number=(self.db.execute('SELECT COALESCE(MAX(number),0)+1 FROM batches').fetchone()[0])
        final=self.directory/f'batch-{number:04d}'
        building=self.directory/f'.building-{number:04d}'
        if final.exists() or building.exists():
            raise ValueError('Diretório de lote já existe; revisão manual necessária: '+str(final))
        (building/'pdfs').mkdir(parents=True)
        selected_shas={item['sha256'] for item in selected}
        for item in selected:
            dest=building/'pdfs'/(item['sha256']+'.pdf')
            try: os.link(item['absolute_path'],dest)
            except OSError:
                free=shutil.disk_usage(self.directory).free
                if free-item['bytes']<self.reserve_bytes:
                    raise RuntimeError('Cópia do lote consumiria a reserva mínima de disco.')
                shutil.copy2(item['absolute_path'],dest)

        source_metadata={row['record_id']:row for row in jsonl(metadata_path)}
        source_results={row['record_id']:row for row in results}
        batch_links=[(record_id,file_sha,url) for record_id,file_sha,url in associations
                     if file_sha in selected_shas]
        record_ids=sorted({row[0] for row in batch_links})
        missing=[record_id for record_id in record_ids if record_id not in source_metadata]
        if missing: raise ValueError('Metadados ausentes para registros do lote.')
        atomic(building/'metadata/records.jsonl',''.join(
            json.dumps(source_metadata[record_id],ensure_ascii=False)+'\n' for record_id in record_ids))
        atomic(building/'results/download_results.jsonl',''.join(
            json.dumps(source_results[record_id],ensure_ascii=False)+'\n' for record_id in record_ids))
        manifest_rows=[dict(record_id=record_id,pdf_path=f'pdfs/{file_sha}.pdf',
                            source_url=url,sha256=file_sha,
                            bytes=files[file_sha]['bytes'])
                       for record_id,file_sha,url in sorted(batch_links)]
        atomic(building/'manifest.jsonl',''.join(
            json.dumps(row,ensure_ascii=False)+'\n' for row in manifest_rows))
        payload_files=sorted(path for path in building.rglob('*') if path.is_file())
        checksums=''.join(f'{sha256(path)}  {path.relative_to(building).as_posix()}\n'
                          for path in payload_files)
        atomic(building/'checksums.sha256',checksums)
        checksums_sha=sha256(building/'checksums.sha256')
        closed=dict(version=VERSION,batch_number=number,closed_at=now(),
                    candidate_pdf_files=len(selected),bytes=selected_bytes,
                    records=len(record_ids),manifest_rows=len(manifest_rows),
                    package_checksum_sha256=checksums_sha,
                    checksum_file='checksums.sha256')
        atomic(building/'CLOSED.json',json.dumps(closed,ensure_ascii=False,indent=2))
        self.verify(building)
        os.replace(building,final)
        with self.db:
            self.db.execute('''INSERT INTO batches
              (number,status,bytes,files,manifest_sha,closed_at,path) VALUES(?,?,?,?,?,?,?)''',
                            (number,'closed',selected_bytes,len(selected),checksums_sha,
                             closed['closed_at'],final.relative_to(self.directory).as_posix()))
            for item in selected:
                self.db.execute('INSERT INTO files VALUES(?,?,?,?)',
                                (item['sha256'],number,f'pdfs/{item["sha256"]}.pdf',item['bytes']))
            for record_id,file_sha,_ in batch_links:
                self.db.execute('INSERT OR REPLACE INTO associations VALUES(?,?,?,?,?)',
                                (record_id,file_sha,number,1,now()))
        self.export_updates(associations)
        return final

    def verify(self,path):
        path=Path(path)
        closed=json.loads((path/'CLOSED.json').read_text(encoding='utf-8'))
        if sha256(path/closed['checksum_file'])!=closed['package_checksum_sha256']:
            raise ValueError('Checksum do pacote não confere.')
        for line in (path/closed['checksum_file']).read_text(encoding='utf-8').splitlines():
            expected,relative=line.split('  ',1)
            target=path/relative
            if not target.exists() or sha256(target)!=expected:
                raise ValueError('Integridade do lote falhou: '+relative)
        return True

    def verify_all(self):
        return [self.verify(self.directory/row[0]) for row in
                self.db.execute("SELECT path FROM batches WHERE status='closed' ORDER BY number")]


def main():
    parser=argparse.ArgumentParser(description='Fecha e verifica lotes imutáveis para cópia ao Drive')
    parser.add_argument('--data-dir',type=Path,default=Path('data/coleta-5000'))
    parser.add_argument('--batch-gb',type=float,default=1.0)
    parser.add_argument('--reserve-gb',type=float,default=10.0)
    parser.add_argument('--force-final',action='store_true')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--prepare-transfer',action='store_true')
    parser.add_argument('--transfer-plan',type=Path)
    parser.add_argument('--mark-transferred',type=int)
    parser.add_argument('--external-location')
    args=parser.parse_args()
    builder=BatchBuilder(args.data_dir/'batches',args.data_dir/'pdfs',
                         int(args.reserve_gb*1024**3),int(args.batch_gb*1024**3))
    try:
        if args.prepare_transfer:
            path=args.transfer_plan or args.data_dir/'reports/batch_transfer_plan.json'
            report=builder.transfer_plan(path)
            print('Plano de transferência:',path.resolve(),'lotes:',report['total_batches'])
        elif args.mark_transferred is not None:
            if not args.transfer_plan or not args.external_location:
                parser.error('--mark-transferred exige --transfer-plan e --external-location.')
            builder.mark_transferred(args.mark_transferred,args.external_location,args.transfer_plan)
            print('Transferência verificada registrada para o lote',args.mark_transferred)
        elif args.verify:
            print('Lotes verificados:',len(builder.verify_all()))
        else:
            path=builder.close_ready(args.data_dir/'pdfs/raw/manifest_pdf.jsonl',
                                     args.data_dir/'metadata/records.jsonl',args.force_final)
            print('Lote fechado:' if path else 'Ainda não há aproximadamente 1 GB pendente:',path or '')
    finally: builder.db.close()


if __name__=='__main__': main()
