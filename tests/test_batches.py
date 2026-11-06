import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from bdtd.batches import BatchBuilder
from bdtd.pdfs import file_hash


class BatchTests(unittest.TestCase):
    def test_closes_verifies_and_never_packages_known_site_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); pdf=root/'pdfs'
            content=b'%PDF-1.4\nwork\n%%EOF\n'; work_sha=hashlib.sha256(content).hexdigest()
            work=pdf/'raw/pdf'/work_sha[:2]/f'{work_sha}.pdf'; work.parent.mkdir(parents=True)
            work.write_bytes(content)
            policy=pdf/'raw/pdf/aa/policy.pdf'; policy.parent.mkdir(parents=True,exist_ok=True)
            policy.write_bytes(b'policy')
            result=dict(record_id='r1',files=[
                dict(path=work.relative_to(pdf).as_posix(),sha256=file_hash(work),bytes=work.stat().st_size,url='https://x/work.pdf'),
                dict(path='raw/pdf/aa/policy.pdf',sha256=file_hash(policy),bytes=6,
                     url='https://x/Politica-de-Informacao-do-Repositorio.pdf')],attempts=[])
            (pdf/'raw').mkdir(exist_ok=True)
            (pdf/'raw/manifest_pdf.jsonl').write_text(json.dumps(result)+'\n',encoding='utf-8')
            metadata=root/'metadata.jsonl'
            metadata.write_text(json.dumps(dict(record_id='r1',title='T'))+'\n',encoding='utf-8')
            builder=BatchBuilder(root/'batches',pdf,reserve_bytes=0,target_bytes=1)
            try:
                path=builder.close_ready(pdf/'raw/manifest_pdf.jsonl',metadata)
                self.assertTrue(builder.verify(path))
                self.assertEqual(len(list((path/'pdfs').iterdir())),1)
                closed=json.loads((path/'CLOSED.json').read_text(encoding='utf-8'))
                self.assertEqual(closed['candidate_pdf_files'],1)
                plan_path=root/'transfer.json'
                plan=builder.transfer_plan(plan_path)
                self.assertEqual(plan['total_batches'],1)
                self.assertEqual(plan['batches'][0]['pdf_count'],1)
                self.assertEqual(plan['batches'][0]['raw_duplicate_pdfs'],1)
                builder.mark_transferred(1,'external://verified/batch-0001',plan_path)
                self.assertEqual(builder.externally_archived_hashes(),{file_hash(work)})
                work.unlink()
                self.assertIsNone(builder.close_ready(pdf/'raw/manifest_pdf.jsonl',metadata))
            finally: builder.db.close()
            (root/'batches/batches.sqlite').unlink()
            resumed=BatchBuilder(root/'batches',pdf,reserve_bytes=0,target_bytes=1)
            self.assertEqual(resumed.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0],1)
            self.assertTrue(resumed.verify_all()[0])
            resumed.db.close()


if __name__=='__main__': unittest.main()
