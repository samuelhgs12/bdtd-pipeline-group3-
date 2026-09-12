"""HTTP do piloto com transportes simulados e arquivos exclusivamente temporários."""
import hashlib
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from bdtd.pdf_http import FetchError, StreamClient


class Response(io.BytesIO):
    status=200

    def __init__(self, body, kind='text/plain', length=True):
        super().__init__(body)
        self.headers=Message()
        self.headers['Content-Type']=kind
        if length: self.headers['Content-Length']=str(len(body))


def http_error(url, status, **headers):
    message=Message()
    for key, value in headers.items(): message[key.replace('_','-')]=value
    return HTTPError(url,status,'fixture',message,io.BytesIO(b''))


class StreamHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client=StreamClient('test@example.org',self.tmp.name)
        self.sleep=patch('bdtd.pdf_http.time.sleep').start()
        self.addCleanup(patch.stopall)

    def error(self, url='https://example.org/item'):
        with self.assertRaises(FetchError) as raised:
            self.client.fetch(url)
        json.dumps(raised.exception.details)
        return raised.exception

    def log(self):
        return [json.loads(line) for line in self.client.log.read_text(encoding='utf-8').splitlines()]

    def test_robots_403_is_cached_without_fetching_either_resource(self):
        denied=http_error('https://example.org/robots.txt',403)
        with patch.object(self.client.opener,'open',side_effect=denied) as opened:
            first=self.error()
            second=self.error('https://example.org/another')
        self.assertEqual(opened.call_count,1)
        self.assertTrue(denied.fp.closed)
        self.assertEqual(first.code,'robots_indisponivel')
        self.assertEqual(first.details,dict(stage='robots',request_url='https://example.org/robots.txt',
            http_status=403,cause='http',policy_cached=False))
        self.assertTrue(second.details['policy_cached'])
        self.assertFalse(self.client.rules)
        self.assertEqual([(r['stage'],r['attempt'],r['status']) for r in self.log()],[('robots',1,403)])

    def test_robots_timeout_and_network_failures_keep_their_cause(self):
        for error, cause in ((TimeoutError('fixture timeout'),'timeout'),
                             (URLError(TimeoutError('fixture timeout')),'timeout'),
                             (URLError('fixture DNS error'),'network')):
            with self.subTest(cause=cause,error=str(error)):
                self.client=StreamClient('test@example.org',self.tmp.name)
                with patch.object(self.client.opener,'open',side_effect=error) as opened:
                    first=self.error()
                    second=self.error('https://example.org/another')
                self.assertEqual(opened.call_count,1)
                self.assertEqual(first.code,'robots_indisponivel')
                self.assertEqual(first.details['cause'],cause)
                self.assertEqual(first.details['stage'],'robots')
                self.assertIsNone(first.details['http_status'])
                self.assertTrue(second.details['policy_cached'])
                self.assertFalse(self.client.rules)
        self.assertEqual(list((Path(self.tmp.name)/'tmp').iterdir()),[])

    def test_new_client_rechecks_failed_policy_in_same_directory(self):
        with patch.object(self.client.opener,'open',side_effect=TimeoutError('fixture')):
            self.error()
        self.client=StreamClient('test@example.org',self.tmp.name)
        with patch.object(self.client.opener,'open',side_effect=[Response(b'User-agent: *\nDisallow: /\n')]) as opened:
            blocked=self.error()
        self.assertEqual(opened.call_count,1)
        self.assertEqual(blocked.code,'robots_bloqueado')
        self.assertFalse(blocked.details['policy_cached'])
        self.assertEqual(len(self.log()),2)

    def test_programming_error_is_not_classified_or_cached_as_network(self):
        with patch.object(self.client.opener,'open',side_effect=TypeError('fixture bug')) as opened:
            for _ in range(2):
                with self.assertRaisesRegex(TypeError,'fixture bug'):
                    self.client.fetch('https://example.org/item')
        self.assertEqual(opened.call_count,2)
        self.assertFalse(self.client.policy_failures)

    def test_redirect_checks_actual_target_policy_and_closes_responses(self):
        robots=Response(b'User-agent: *\nDisallow:\n')
        redirect=http_error('https://example.org/item',302,Location='https://other.example/file.pdf')
        denied=http_error('https://other.example/robots.txt',403)
        with patch.object(self.client.opener,'open',side_effect=[robots,redirect,denied]) as opened:
            failure=self.error()
        self.assertEqual([call.args[0].full_url for call in opened.call_args_list],
            ['https://example.org/robots.txt','https://example.org/item','https://other.example/robots.txt'])
        self.assertEqual(failure.details['request_url'],'https://other.example/robots.txt')
        self.assertTrue(robots.closed)
        self.assertTrue(redirect.fp.closed)
        self.assertTrue(denied.fp.closed)

    def test_robots_redirect_failure_caches_for_original_origin(self):
        redirect=http_error('https://example.org/robots.txt',301,Location='https://policy.example/robots.txt')
        with patch.object(self.client.opener,'open',side_effect=[redirect,TimeoutError('fixture')]) as opened:
            first=self.error()
            cached=self.error('https://example.org/another')
        self.assertEqual(opened.call_count,2)
        self.assertEqual(first.details['request_url'],'https://policy.example/robots.txt')
        self.assertEqual(first.code,'robots_indisponivel')
        self.assertTrue(cached.details['policy_cached'])

    def test_redirect_loop_is_bounded_and_not_permission(self):
        redirect=http_error('https://example.org/robots.txt',302,Location='/robots.txt')
        with patch.object(self.client.opener,'open',side_effect=redirect) as opened:
            failure=self.error()
            self.error('https://example.org/another')
        self.assertEqual(opened.call_count,1)
        self.assertEqual(failure.details['cause'],'redirect_loop')
        self.assertFalse(self.client.rules)

    def test_retry_after_is_distinct_cached_and_does_not_sleep_long(self):
        busy=http_error('https://example.org/robots.txt',503,Retry_After='120')
        with patch.object(self.client.opener,'open',side_effect=busy) as opened:
            first=self.error()
            second=self.error('https://example.org/another')
        self.assertEqual(opened.call_count,1)
        self.assertEqual(first.code,'retry_after')
        self.assertEqual(first.details['http_status'],503)
        self.assertEqual(first.details['stage'],'robots')
        self.assertTrue(second.details['policy_cached'])
        self.assertTrue(busy.fp.closed)
        self.assertTrue(all(call.args[0]<=60 for call in self.sleep.call_args_list))

    def test_transient_http_retries_are_bounded_and_logged(self):
        errors=[http_error('https://example.org/robots.txt',503) for _ in range(3)]
        with patch.object(self.client.opener,'open',side_effect=errors) as opened:
            first=self.error()
            self.error('https://example.org/another')
        self.assertEqual(opened.call_count,3)
        self.assertEqual(first.code,'robots_indisponivel')
        self.assertEqual([entry['attempt'] for entry in self.log()],[1,2,3])
        self.assertTrue(all(error.fp.closed for error in errors))

    def test_retry_can_recover_and_404_confirms_missing_policy(self):
        busy=http_error('https://example.org/robots.txt',503)
        missing=http_error('https://example.org/robots.txt',404)
        with patch.object(self.client.opener,'open',side_effect=[busy,missing,Response(b'fixture')]) as opened:
            result=self.client.fetch('https://example.org/item')
        self.assertEqual(opened.call_count,3)
        self.assertEqual(result['robots_policy']['robots_http_status'],404)
        self.assertEqual(Path(result['path']).read_bytes(),b'fixture')
        self.assertTrue(busy.fp.closed)
        self.assertTrue(missing.fp.closed)

    def test_policy_artifact_and_request_are_correlated_in_log_and_result(self):
        body=b'User-agent: *\nDisallow:\n'
        with patch.object(self.client.opener,'open',side_effect=[Response(body),Response(b'fixture'),Response(b'next')]) as opened:
            result=self.client.fetch('https://example.org/item')
            cached=self.client.fetch('https://example.org/another')
        self.assertEqual(opened.call_count,3)
        evidence=result['robots_policy']
        self.assertEqual(evidence['robots_sha256'],hashlib.sha256(body).hexdigest())
        self.assertEqual((Path(self.tmp.name)/evidence['robots_path']).read_bytes(),body)
        self.assertFalse(evidence['policy_cached'])
        self.assertTrue(cached['robots_policy']['policy_cached'])
        logs=self.log()
        self.assertEqual(logs[0]['sha256'],evidence['robots_sha256'])
        self.assertEqual(logs[1]['robots_path'],evidence['robots_path'])
        self.assertEqual(logs[1]['robots_url'],logs[0]['url'])

    def test_html_policy_is_archived_but_does_not_authorize_resource(self):
        body=b'<!doctype html><html>Access denied</html>'
        with patch.object(self.client.opener,'open',return_value=Response(body,'text/html')) as opened:
            first=self.error()
            self.error('https://example.org/another')
        self.assertEqual(opened.call_count,1)
        self.assertEqual(first.details['cause'],'invalid_policy')
        self.assertEqual((Path(self.tmp.name)/first.details['robots_path']).read_bytes(),body)
        self.assertFalse(self.client.rules)

    def test_resource_timeout_keeps_resource_stage_and_policy_evidence(self):
        with patch.object(self.client.opener,'open',side_effect=[Response(b''),TimeoutError('fixture')]):
            failure=self.error()
        self.assertEqual(failure.code,'timeout')
        self.assertEqual(failure.details['stage'],'resource')
        self.assertEqual(failure.details['request_url'],'https://example.org/item')
        self.assertIn('robots_sha256',failure.details)
        self.assertFalse(self.client.policy_failures)

    def test_exact_budget_stops_new_requests_before_policy(self):
        self.client.transferred=self.client.budget
        with patch.object(self.client.opener,'open') as opened:
            failure=self.error()
        self.assertEqual(failure.code,'limite_execucao')
        opened.assert_not_called()

    def test_robots_bytes_can_exhaust_budget_without_resource_request(self):
        self.client.budget=1
        with patch.object(self.client.opener,'open',return_value=Response(b'\n')) as opened:
            failure=self.error()
        self.assertEqual(opened.call_count,1)
        self.assertEqual(self.client.transferred,1)
        self.assertEqual(failure.code,'limite_execucao')
        self.assertEqual(failure.details['stage'],'resource')

    def test_unknown_length_never_reads_beyond_execution_budget(self):
        self.client.budget=5
        with patch.object(self.client.opener,'open',return_value=Response(b'0123456789',length=False)):
            with self.assertRaises(FetchError) as raised:
                self.client._once('https://example.org/resource',100)
        self.assertEqual(raised.exception.code,'limite_execucao')
        self.assertEqual(self.client.transferred,5)
        self.assertEqual(list((Path(self.tmp.name)/'tmp').iterdir()),[])

    def test_file_limit_in_robots_remains_distinct_and_is_not_negative_cached(self):
        response=Response(b'')
        response.headers.replace_header('Content-Length',str(1024*1024+1))
        with patch.object(self.client.opener,'open',return_value=response):
            failure=self.error()
        self.assertEqual(failure.code,'limite_arquivo')
        self.assertEqual(failure.details['stage'],'robots')
        self.assertFalse(self.client.policy_failures)

    def test_known_length_can_finish_at_exact_budget(self):
        self.client.budget=3
        with patch.object(self.client.opener,'open',return_value=Response(b'abc')) as opened:
            result=self.client._once('https://example.org/resource',100)
            with self.assertRaises(FetchError) as raised:
                self.client._once('https://example.org/another',100)
        self.assertEqual(Path(result['path']).read_bytes(),b'abc')
        self.assertEqual(opened.call_count,1)
        self.assertEqual(raised.exception.code,'limite_execucao')


if __name__=='__main__': unittest.main()
