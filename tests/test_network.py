import io
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

from bdtd.network import Client


class FailingOpener:
    def __init__(self,error): self.error=error; self.calls=0
    def open(self,request,timeout):
        self.calls+=1
        raise self.error


class NetworkEvidenceTests(unittest.TestCase):
    def test_429_is_single_network_call_and_logs_safe_response_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            headers=Message()
            headers['Retry-After']='120'
            headers['Content-Type']='application/json; charset=utf-8'
            headers['Set-Cookie']='secret=must-not-be-logged'
            error=urllib.error.HTTPError('https://example.org/api',429,'Too Many Requests',headers,
                                         io.BytesIO(b'{"message":"slow down"}'))
            log=Path(directory)/'http.jsonl'
            client=Client('test',interval=1,log=log,retry_429=False)
            client.last['example.org']=-100
            client.opener=FailingOpener(error)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                client.request('https://example.org/api')
            self.assertEqual(client.opener.calls,1)
            event=json.loads(log.read_text(encoding='utf-8').strip())
            self.assertEqual(event['source'],'network')
            self.assertEqual(event['retry_after'],'120')
            self.assertEqual(event['content_type'],'application/json; charset=utf-8')
            self.assertIn('slow down',event['body_excerpt'])
            self.assertNotIn('secret',json.dumps(event))
            self.assertEqual(caught.exception.bdtd_evidence['retry_after'],'120')
            caught.exception.close()


if __name__=='__main__': unittest.main()
