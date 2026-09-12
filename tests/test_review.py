import json
import tempfile
import unittest
from pathlib import Path

from bdtd.core import digest
from bdtd.review import build_review


class ReviewTests(unittest.TestCase):
    def test_all_records_are_joined_and_final_decision_stays_blank(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = [dict(record_id=f'id-{i}', title=f'Título {i}', abstracts=[],
                           subjects=['Linguagem'], urls=['https://example.org'],
                           sample_order=i, sample_rank=i) for i in range(1, 51)]
            sample_bytes = ''.join(json.dumps(row) + '\n' for row in sample).encode()
            (root / 'sample.jsonl').write_bytes(sample_bytes)
            results = [dict(record_id=f'id-{i}', status='sucesso' if i == 1 else 'nao_obtido',
                            files=[dict(sha256='a', bytes=10)] if i == 1 else [], attempts=[])
                       for i in range(1, 51)]
            (root / 'manifest.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in results), encoding='utf-8')
            metadata = dict(record_id='id-1', fields={'dcterms.abstract': ['Resumo disponível.']})
            (root / 'metadata.jsonl').write_text(json.dumps(metadata) + '\n', encoding='utf-8')
            triage = dict(sample_sha256=digest(sample_bytes), classifications=[
                dict(record_id=f'id-{i}', classification='pertinente', justification='Tema explícito.')
                for i in range(1, 51)])
            (root / 'triage.json').write_text(json.dumps(triage), encoding='utf-8')
            report = build_review(root / 'sample.jsonl', root / 'manifest.jsonl',
                                  root / 'metadata.jsonl', root / 'triage.json', root / 'out')
            rows = [json.loads(line) for line in
                    (root / 'out/review_50.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows), 50)
            self.assertEqual(len((root / 'out/review_50.csv').read_text(encoding='utf-8-sig').splitlines()), 51)
            self.assertEqual(rows[0]['abstract'], 'Resumo disponível.')
            self.assertTrue(all(row['final_decision'] == '' for row in rows))
            self.assertEqual(report['download']['works_with_at_least_one_pdf'], 1)
            self.assertFalse(report['preliminary_relevance']['human_validation'])


if __name__ == '__main__':
    unittest.main()
