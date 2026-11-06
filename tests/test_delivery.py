import json
import tempfile
import unittest
from pathlib import Path

from bdtd.delivery import tree_hash,verify


class DeliveryTests(unittest.TestCase):
    def test_tree_verification_detects_copy_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); file=root/'batches/batch-0001/a.pdf'
            file.parent.mkdir(parents=True); file.write_bytes(b'pdf')
            import hashlib
            item=dict(destination_path='batches/batch-0001/a.pdf',bytes=3,
                      sha256=hashlib.sha256(b'pdf').hexdigest())
            inventory=root/'inventory.json'; inventory.write_text(json.dumps(dict(
                groups=[dict(id='batch-0001',files=[item],tree_sha256=tree_hash([item]))])),encoding='utf-8')
            self.assertEqual(verify(root,inventory),1)
            file.write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError,'divergente'):
                verify(root,inventory)


if __name__=='__main__': unittest.main()
