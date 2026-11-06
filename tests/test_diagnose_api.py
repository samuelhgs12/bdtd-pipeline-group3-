import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bdtd.api import build_url
from bdtd.diagnose_api import active_cooldown, add_response_summary, diagnostic_config, main
from bdtd.collect import Collector
from unittest.mock import patch


class DiagnosticTests(unittest.TestCase):
    def test_url_has_exact_date_range_and_no_facets(self):
        config = diagnostic_config(dict(endpoint='https://example.org/api', join='AND', groups=[
            dict(operator='OR', clauses=[dict(lookfor='Linguagem', type='Subject'),
                                         dict(lookfor='Linguagem', type='Abstract'),
                                         dict(lookfor='Comunicação', type='Subject'),
                                         dict(lookfor='Comunicação', type='Abstract')])],
            filters=['~format:"masterThesis"', '~format:"doctoralThesis"'], facets=['x'], limit=100))
        params = parse_qs(urlsplit(build_url(config, 2)).query)
        self.assertEqual(params['daterange[]'], ['publishDate'])
        self.assertEqual(params['publishDatefrom'], ['2024'])
        self.assertEqual(params['publishDateto'], ['2024'])
        self.assertEqual(params['limit'], ['20'])
        self.assertEqual(params['page'], ['2'])
        self.assertNotIn('facet[]', params)
        self.assertEqual(params['filter[]'],['~format:"masterThesis"','~format:"doctoralThesis"'])
        self.assertEqual(params['type0[]'], ['Subject', 'Abstract', 'Subject', 'Abstract'])

    def test_response_summary_reports_ids_and_page_one_overlap(self):
        body=json.dumps({'status':'OK','resultCount':8616,'records':[
            {'id':'same','title':'A'},{'id':'new','title':'B'}]}).encode()
        report={}
        add_response_summary(report,body,['first','same'])
        self.assertEqual(report['result_count'],8616)
        self.assertEqual(report['records'],2)
        self.assertEqual(report['ids'],['same','new'])
        self.assertEqual(report['overlap_with_page_1'],['same'])

    def test_active_cooldown_is_read_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'collection.sqlite'
            db = sqlite3.connect(database)
            db.execute('CREATE TABLE api_cooldowns(origin TEXT PRIMARY KEY,blocked_until TEXT,observed_at TEXT,url TEXT,wait_source TEXT,event_id INTEGER)')
            deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db.execute('INSERT INTO api_cooldowns VALUES(?,?,?,?,?,?)',
                       ('https://example.org', deadline, deadline, 'https://example.org/api', 'test', 1))
            db.commit()
            db.close()
            self.assertEqual(active_cooldown(database, 'https://example.org/api')['blocked_until'], deadline)

    def test_command_exits_on_shared_active_cooldown_without_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); config={'endpoint':'https://example.org/api','lookfor':'x','type':'Subject',
                                    'filters':['~format:"masterThesis"'],'limit':20}
            config_path=root/'config.json'; config_path.write_text(json.dumps(config),encoding='utf-8')
            collector=Collector(root,config)
            observed=datetime.now(timezone.utc)
            collector.record_rate_limit(config['endpoint'],None,observed+timedelta(hours=1),
                                        'client_policy_no_retry_after',{},observed.isoformat())
            collector.db.close()
            argv=['diagnose_api','--contact','test@example.org','--config',str(config_path),
                  '--data-dir',str(root),'--page','2']
            with patch('sys.argv',argv),patch('bdtd.diagnose_api.Client') as client:
                main()
            client.assert_not_called()
            report=json.loads((root/'reports/api_diagnostic_2024_page_2.json').read_text(encoding='utf-8'))
            self.assertEqual(report['error'],'cooldown_ativo_sem_requisicao')


if __name__ == '__main__':
    unittest.main()
