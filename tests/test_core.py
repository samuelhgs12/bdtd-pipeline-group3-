import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bdtd.core import atomic


class AtomicWriteTests(unittest.TestCase):
    def test_transient_permission_error_is_retried_after_file_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'manifest_pdf.jsonl'
            real_replace=os.replace
            calls=[]
            def transient(source,destination):
                calls.append((source,destination))
                if len(calls)<3:
                    raise PermissionError(5,'Acesso negado')
                return real_replace(source,destination)
            with patch('bdtd.core.os.replace',side_effect=transient),patch('bdtd.core.time.sleep'):
                atomic(target,'linha\n')
            self.assertEqual(len(calls),3)
            self.assertEqual(target.read_text(encoding='utf-8'),'linha\n')

    def test_persistent_permission_error_preserves_exception_and_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'manifest_pdf.jsonl'
            target.write_text('anterior\n',encoding='utf-8')
            legacy=target.with_suffix(target.suffix+'.tmp')
            legacy.write_text('interrupção anterior\n',encoding='utf-8')
            with patch('bdtd.core.os.replace',side_effect=PermissionError(5,'Acesso negado')), \
                 patch('bdtd.core.time.sleep') as sleeping:
                with self.assertRaises(PermissionError):
                    atomic(target,'novo\n')
            self.assertEqual(sleeping.call_count,5)
            self.assertEqual(target.read_text(encoding='utf-8'),'anterior\n')
            self.assertEqual(legacy.read_text(encoding='utf-8'),'interrupção anterior\n')
            unique=[path for path in target.parent.glob(target.name+'.*.tmp')]
            self.assertEqual(len(unique),1)
            self.assertEqual(unique[0].read_text(encoding='utf-8'),'novo\n')


if __name__=='__main__':
    unittest.main()
