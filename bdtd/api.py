"""Consulta à API de busca da BDTD: monta URLs, valida páginas e normaliza registros."""
import json
from urllib.parse import urlencode


def usable_urls(row):
    urls=row.get('urls',[]) if isinstance(row,dict) else []
    return [item['url'] for item in urls if isinstance(item,dict) and
            isinstance(item.get('url'),str) and item['url'].startswith(('http://','https://'))]


def title_issue(row):
    if 'title' not in row or row.get('title') is None: return 'titulo_ausente'
    if isinstance(row['title'],str) and not row['title'].strip(): return 'titulo_vazio'
    if not isinstance(row['title'],str): return 'titulo_tipo_inesperado'
    return None


def validate(data,allow_record_rejections=False):
    obj = json.loads(data)
    if not isinstance(obj, dict) or obj.get('status') != 'OK':
        raise ValueError('API não retornou status OK.')
    total, records = obj.get('resultCount'), obj.get('records')
    if type(total) is not int or total < 0 or not isinstance(records, list):
        raise ValueError('resultCount/records inválidos.')
    ids = []; accepted=[]; rejections=[]
    for index,row in enumerate(records):
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
            if allow_record_rejections:
                rejections.append(dict(position=index,reason='record_id_ausente_ou_invalido',payload=row))
                continue
            raise ValueError('Registro sem ID válido.')
        issue=title_issue(row)
        if issue:
            if allow_record_rejections and not usable_urls(row):
                rejections.append(dict(position=index,reason=issue+'_e_sem_url_utilizavel',payload=row))
                continue
            if not allow_record_rejections:
                raise ValueError('Registro sem título textual.')
        ids.append(row['id'])
        accepted.append(row)
    if len(ids) != len(set(ids)):
        raise ValueError('IDs repetidos na mesma página.')
    if allow_record_rejections:
        obj=dict(obj,records=accepted,_record_rejections=rejections,
                 _source_record_count=len(records))
    return obj

def strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return []

def normalize(row, provenance):
    authors = row.get('authors', {})
    primary = authors.get('primary', {}) if isinstance(authors, dict) else {}
    urls = row.get('urls', [])
    if not isinstance(urls, list):
        urls = []
    abstracts = strings(row.get('abstracts', row.get('abstract', row.get('description', []))))
    issue=title_issue(row)
    title=row.get('title') if issue is None else None
    quality=[] if issue is None else [issue]
    return dict(record_id=row['id'], title=title,metadata_quality_flags=quality,
                authors=list(primary) if isinstance(primary, dict) else strings(primary),
                formats=strings(row.get('formats', [])), languages=strings(row.get('languages', [])),
                subjects=strings(row.get('subjects', [])), abstracts=abstracts,
                urls=[u['url'] for u in urls if isinstance(u, dict) and isinstance(u.get('url'), str)],
                area_status='consulta_exploratoria_nao_validada', metadata_level='search',
                original_record=row, **provenance)

def build_url(config, page, limit=None):
    params = []
    if config.get('groups'):
        params.append(('join', config.get('join', 'AND')))
        for index, group in enumerate(config['groups']):
            params.append((f'bool{index}[]', group['operator']))
            for clause in group['clauses']:
                params.append((f'lookfor{index}[]', clause['lookfor']))
                params.append((f'type{index}[]', clause['type']))
    else:
        params += [('lookfor', config['lookfor']), ('type', config['type'])]
    params += [('page', str(page)), ('limit', str(limit if limit is not None else config['limit']))]
    params += [('filter[]', value) for value in config['filters']]
    params += [('facet[]', value) for value in config.get('facets', [])]
    params += [('daterange[]', value) for value in config.get('date_ranges', [])]
    for name in ('publishDatefrom', 'publishDateto'):
        if name in config:
            params.append((name, str(config[name])))
    return config['endpoint'] + '?' + urlencode(params)
