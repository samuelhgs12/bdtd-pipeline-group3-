"""Primeira etapa: Raw imutável, importação OAI e Staging rastreável."""
import hashlib
import json
import os
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

VERSION = '0.1.0'
NS = {'o': 'http://www.openarchives.org/OAI/2.0/',
      'dc': 'http://purl.org/dc/elements/1.1/',
      'd': 'http://www.openarchives.org/OAI/2.0/oai_dc/'}
FIELDS = ['title', 'creator', 'contributor', 'description', 'subject',
          'publisher', 'date', 'type', 'language', 'rights', 'identifier']

def now():
    return datetime.now(timezone.utc).isoformat()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text, encoding='utf-8')
    os.replace(temp, path)

def parse_oai(data, verb=None):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('XML com DTD/entidades não é aceito.')
    root = ET.fromstring(data)
    if root.tag != '{' + NS['o'] + '}OAI-PMH':
        raise ValueError('Resposta não é OAI-PMH; pode ser HTML de bloqueio.')
    error = root.find('o:error', NS)
    if error is not None:
        if error.get('code') == 'noRecordsMatch' and verb == 'ListRecords':
            return root
        raise ValueError(f"OAI {error.get('code')}: {error.text}")
    if verb and root.find('o:' + verb, NS) is None:
        raise ValueError('Resposta não contém ' + verb)
    return root

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'state.sqlite')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS raw (
          sha TEXT PRIMARY KEY, path TEXT, source TEXT, ts TEXT, synthetic INTEGER);
        CREATE TABLE IF NOT EXISTS parsed (sha TEXT PRIMARY KEY, version TEXT);
        CREATE TABLE IF NOT EXISTS records (
          id TEXT PRIMARY KEY, datestamp TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS rejects (
          sha TEXT, position INTEGER, reason TEXT, PRIMARY KEY(sha, position));
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, token TEXT, done INTEGER);
        ''')

    def archive(self, data, source, synthetic=False):
        sha = digest(data)
        path = self.directory / 'raw' / 'oai' / (sha + '.xml')
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open('xb') as f:
                f.write(data)
        elif digest(path.read_bytes()) != sha:
            raise ValueError('Raw corrompido: ' + str(path))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO raw VALUES(?,?,?,?,?)',
                            (sha, str(path.relative_to(self.directory)), source, now(), int(synthetic)))
        return sha

    def staging(self):
        for sha, relative, source, ts, synthetic in self.db.execute('SELECT * FROM raw').fetchall():
            if self.db.execute('SELECT 1 FROM parsed WHERE sha=? AND version=?', (sha, VERSION)).fetchone():
                continue
            data = (self.directory / relative).read_bytes()
            if digest(data) != sha:
                raise ValueError('Checksum inválido: ' + relative)
            root = parse_oai(data, 'ListRecords')
            with self.db:
                for i, rec in enumerate(root.findall('o:ListRecords/o:record', NS)):
                    rid = rec.findtext('o:header/o:identifier', '', NS).strip()
                    stamp = rec.findtext('o:header/o:datestamp', '', NS).strip()
                    header = rec.find('o:header', NS)
                    deleted = header is not None and header.get('status') == 'deleted'
                    dc = rec.find('o:metadata/d:dc', NS)
                    if not rid or not stamp or (not deleted and dc is None):
                        self.db.execute('INSERT OR REPLACE INTO rejects VALUES(?,?,?)',
                                        (sha, i, 'identifier/datestamp/DC ausente'))
                        continue
                    fields = {key: [] for key in FIELDS}
                    if dc is not None:
                        for key in FIELDS:
                            fields[key] = [{'value': ''.join(el.itertext()),
                                            'lang': el.get('{http://www.w3.org/XML/1998/namespace}lang')}
                                           for el in dc.findall('dc:' + key, NS)]
                    row = dict(record_id=rid, datestamp=stamp, deleted=deleted,
                               dc=fields, sets=[x.text for x in rec.findall('o:header/o:setSpec', NS)],
                               source_url=source, harvest_ts=ts, sha256=sha,
                               pipeline_version=VERSION, synthetic=bool(synthetic),
                               area_status='nao_validada')
                    self.db.execute('''INSERT INTO records VALUES(?,?,?)
                        ON CONFLICT(id) DO UPDATE SET datestamp=excluded.datestamp,
                        payload=excluded.payload WHERE excluded.datestamp >= records.datestamp''',
                        (rid, stamp, json.dumps(row, ensure_ascii=False)))
                self.db.execute('INSERT OR REPLACE INTO parsed VALUES(?,?)', (sha, VERSION))
        self.export()

    def export(self):
        target = self.directory / 'staging' / 'metadados.jsonl'
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as f:
            for (payload,) in self.db.execute('SELECT payload FROM records ORDER BY id'):
                f.write(payload + '\n')
        os.replace(temp, target)
        raw = [dict(sha256=r[0], path=r[1], source_url=r[2], harvest_ts=r[3], synthetic=bool(r[4]))
               for r in self.db.execute('SELECT * FROM raw ORDER BY sha')]
        atomic(self.directory / 'raw' / 'manifest.jsonl', ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in raw))
        rejected = [dict(sha256=r[0], position=r[1], reason=r[2]) for r in self.db.execute('SELECT * FROM rejects')]
        atomic(self.directory / 'staging' / 'rejeitados.jsonl', ''.join(json.dumps(r)+'\n' for r in rejected))
        count = self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        atomic(self.directory / 'reports' / 'status.json', json.dumps(dict(
            version=VERSION, raw_pages=len(raw), records=count, rejected=len(rejected),
            area_validated=False, curated_ready=False), indent=2))
        return count
