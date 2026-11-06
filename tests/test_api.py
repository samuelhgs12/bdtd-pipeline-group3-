import json
import unittest
from bdtd.api import normalize, validate, build_url
from urllib.parse import parse_qs, urlsplit

CONFIG = {'endpoint':'https://example.org/api', 'lookfor':'Linguagens OR Comunicação',
          'type':'Subject', 'filters':['~format:"masterThesis"','~format:"doctoralThesis"'],
          'limit':2, 'browser_reference_total':4}

def response(ids, total=4):
    return json.dumps(dict(status='OK',resultCount=total, records=[dict(id=str(i),title='Título '+str(i),
               formats=['masterThesis'],subjects=[['Comunicação'], 'Linguagens'],urls=[]) for i in ids])).encode()

class ApiTests(unittest.TestCase):
    def test_invalid_status_is_stopped(self):
        with self.assertRaises(ValueError): validate(b'{"status":"ERROR"}')
        with self.assertRaises(ValueError): validate(response([1,1]))
    def test_filters_and_subject_query_preserved(self):
        params=parse_qs(urlsplit(build_url(CONFIG,2)).query)
        self.assertEqual(params['filter[]'],CONFIG['filters'])
        self.assertEqual(params['type'],['Subject'])
        self.assertEqual(params['lookfor'],['Linguagens OR Comunicação'])

    def test_advanced_group_preserves_fields_and_or_operator(self):
        config=dict(CONFIG, groups=[dict(operator='OR',clauses=[
            dict(lookfor='Linguagem',type='Subject'),
            dict(lookfor='Linguagem',type='Abstract'),
            dict(lookfor='Comunicação',type='Subject'),
            dict(lookfor='Comunicação',type='Abstract')])],join='AND')
        config.pop('lookfor'); config.pop('type')
        params=parse_qs(urlsplit(build_url(config,3)).query)
        self.assertEqual(params['join'],['AND'])
        self.assertEqual(params['bool0[]'],['OR'])
        self.assertEqual(params['lookfor0[]'],['Linguagem','Linguagem','Comunicação','Comunicação'])
        self.assertEqual(params['type0[]'],['Subject','Abstract','Subject','Abstract'])
        self.assertEqual(params['page'],['3'])
    def test_optional_facets_are_repeated(self):
        config=dict(CONFIG,facets=['publishDate','institution'])
        params=parse_qs(urlsplit(build_url(config,1)).query)
        self.assertEqual(params['facet[]'],['publishDate','institution'])
    def test_normalization_keeps_original(self):
        row=validate(response([1,2]))['records'][0]
        out=normalize(row,{})
        self.assertEqual(out['subjects'],['Comunicação','Linguagens'])
        self.assertEqual(out['original_record'],row)
        self.assertEqual(out['urls'],[])
