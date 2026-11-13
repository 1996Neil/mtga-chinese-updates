"""Versioned, append-only evidence. No game writes and no AI semantic approval."""
import datetime
import difflib
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import urllib.request
from urllib.parse import urlparse
from common import STATE, atomic_bytes, database, fingerprint, has_chinese, require, safe_format, save_json, managed_path, regular

TABLES = {
 'provenance_documents': 'id TEXT PRIMARY KEY, payload TEXT NOT NULL',
 'provenance_translations': 'id TEXT PRIMARY KEY, layer TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL',
 'provenance_reviews': 'id TEXT PRIMARY KEY, translation_id TEXT NOT NULL, payload TEXT NOT NULL',
 'provenance_observations': 'id TEXT PRIMARY KEY, payload TEXT NOT NULL',
 'provenance_selection_cache': 'id TEXT PRIMARY KEY, translation_id TEXT NOT NULL, context_hash TEXT NOT NULL, evidence_hash TEXT NOT NULL, payload TEXT NOT NULL',
 'provenance_migrations': 'id TEXT PRIMARY KEY, payload TEXT NOT NULL',
}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def append(c, table, value, **columns):
    require(table in TABLES, 'Unknown provenance table')
    payload = canonical(value)
    ident = hash_text(payload)
    names = ['id', *columns, 'payload']
    c.execute(f'INSERT OR IGNORE INTO {table} ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
              [ident, *columns.values(), payload])
    return ident

def migrate(c, backup_dir=None):
    """Transactional additive DDL, including restart after an interrupted older migration."""
    expected={f'{t}_{action}' for t in TABLES for action in ('insert','update','delete')}
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    triggers={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if set(TABLES)<=tables and expected<=triggers:
        return
    require(not c.in_transaction, 'Commit legacy work before provenance migration')
    path=c.execute('PRAGMA database_list').fetchone()[2]
    backup=None
    if path:
        directory=Path(backup_dir or (Path(path).parent/'registry-backups'))
        directory.mkdir(parents=True,exist_ok=True)
        backup=directory/('before-provenance-'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')+'.sqlite')
        require(not backup.exists(),'Backup already exists')
        with sqlite3.connect(backup) as dst:
            c.backup(dst)
            require(dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok','Registry backup invalid')
    c.execute('BEGIN IMMEDIATE')
    try:
        for table,schema in TABLES.items():
            c.execute(f'CREATE TABLE IF NOT EXISTS {table} ({schema})')
            columns=[r[1] for r in c.execute(f'PRAGMA table_info({table})')]
            require(columns==[x.split()[0] for x in schema.split(',')],'Unknown provenance schema')
            for action in ('UPDATE','DELETE'):
                c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append-only provenance'); END")
            comparisons=' AND '.join(f'{col} IS NEW.{col}' for col in columns)
            c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_insert BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'append-only provenance identity conflict') WHERE NOT EXISTS(SELECT 1 FROM {table} WHERE {comparisons}); SELECT RAISE(IGNORE); END")
        append(c,'provenance_migrations',{'version':2,'at':now(),'backup':str(backup) if backup else None,
                                         'backup_hash':fingerprint(backup) if backup else None})
        c.commit()
    except BaseException:
        c.rollback()
        raise

def official_url(url):
    p = urlparse(url)
    return p.scheme == 'https' and p.hostname in {'magic.wizards.com', 'media.wizards.com', 'gatherer.wizards.com'} and not p.username

def import_pack(c, pack_path, review_path, cache=None):
    """Independent review verifies extraction/identity ONLY, not translation semantics."""
    pack_path, review_path = regular(pack_path), regular(review_path)
    pack, review = json.loads(pack_path.read_text()), json.loads(review_path.read_text())
    require(review.get('kind') == 'independent-source-verification' and review.get('status') == 'PASS'
            and review.get('pack_fingerprint') == fingerprint(pack_path) and bool(review.get('reviewer')),
            'Source pack needs independent, hash-bound extraction verification')
    def has_human_claim(value):
        if isinstance(value,dict):
            return value.get('review_kind')=='human' or any(has_human_claim(v) for v in value.values())
        return isinstance(value,list) and any(has_human_claim(v) for v in value)
    require(pack.get('schema') == 'MTGA_SOURCE_PACK_V1' and not has_human_claim(pack) and not has_human_claim(review), 'Not a human approval channel')
    cache = managed_path(cache or STATE / 'source-cache'); cache.mkdir(parents=True, exist_ok=True)
    docs = {}
    # Validate everything before the transaction; immutable cached blobs are harmless if import aborts.
    for item in pack['documents']:
        path = pack_path.parent / item['file']
        require(fingerprint(path) == item['fingerprint'], 'Source document changed')
        require(item['source_type'] in {'official', 'community', 'local-donor'}, 'Invalid source type')
        require(item['source_type'] != 'official' or official_url(item['url']), 'Official source must have an official URL')
        require(item.get('retrieved_at') and item.get('language') and 'published_at' in item, 'Source dates/language required')
        target = cache / item['fingerprint']['sha256']
        if not target.exists():
            atomic_bytes(target, path.read_bytes())
        require(fingerprint(target) == item['fingerprint'], 'Cached source changed')
        docs[item['label']] = {**item, 'cache_path': str(target.resolve()), 'verification': review}
    records = []
    for row in pack['translations']:
        require(row.get('review_kind') != 'human' and row.get('status', 'candidate') == 'candidate', 'Pack cannot approve a translation')
        require(row['layer'] in {'card', 'client', 'ui'} and row['field_use'] and row['purpose'] in
                {'card-name', 'rules', 'reminder', 'flavor', 'UI'}, 'Invalid translation use')
        require(row['language'] in {'zh-Hans', 'zh-Hant'} and has_chinese(row['translation']), 'Explicit Chinese variant required')
        require(row['document'] in docs and row.get('source_english') and row.get('identity'), 'Incomplete translation evidence')
        oracle = row.get('oracle')
        if row.get('source_english_document'):
            require(row['source_english_document'] in docs, 'Missing paired English source')
        if oracle:
            require(oracle['document'] in docs and oracle.get('text') and oracle.get('version'), 'Incomplete Oracle evidence')
        records.append(row)
    with c:
        ids = {label: append(c, 'provenance_documents', item) for label, item in docs.items()}
        for row in records:
            item = {**row, 'document_id': ids[row['document']], 'source_english_hash': hash_text(row['source_english']),
                    'verification': review, 'first_seen_version': pack['client_version']}
            if row.get('source_english_document'):
                item['source_english_document_id'] = ids[row['source_english_document']]
            if row.get('oracle'):
                item['oracle'] = {**row['oracle'], 'document_id': ids[row['oracle']['document']],
                                  'hash': hash_text(row['oracle']['text'])}
            append(c, 'provenance_translations', item, layer=row['layer'], key=str(row['key']))
    return len(records)

def load_rows(c):
    return [(i, json.loads(p)) for i, p in c.execute('SELECT id,payload FROM provenance_translations ORDER BY id')]

def evidence_index(c, current):
    """Read-only discovery of observations. Discovery never grants admission."""
    docs = {i: json.loads(p) for i,p in c.execute('SELECT id,payload FROM provenance_documents')}
    result = {}
    cache = {}
    for ident, payload in c.execute('SELECT id,payload FROM provenance_observations ORDER BY id'):
        event = json.loads(payload)
        if event.get('schema') != 'v1.2-round1-review-observation' or not event.get('associated_source_document_ids'):
            continue
        row = event.get('result', {})
        key = (event['layer'], str(event['key']))
        context = current.get(key)
        binding = bool(context and row.get('current_english_hash') == hash_text(context['english'])
                       and row.get('identity') == context['identity'] and row.get('field_use') == context['field_use'])
        linked, kinds = [], set()
        for doc_id in event['associated_source_document_ids']:
            doc = docs.get(doc_id)
            if doc_id not in cache:
                try:
                    cache[doc_id] = bool(doc and fingerprint(Path(doc['cache_path'])) == doc['fingerprint'])
                except (OSError, KeyError, ValueError, RuntimeError): cache[doc_id] = False
            meta = doc.get('metadata', doc) if doc else {}
            language = meta.get('language', meta.get('printed_language'))
            chinese = language in {'zh-Hans','zh-Hant','zhs','zht','zh-CN','zh-TW','CS','CT'}
            if chinese and doc: kinds.add(doc['source_type'])
            linked.append({'id': doc_id, 'cache_valid': cache[doc_id], 'language': language,
                           'source_type': doc.get('source_type') if doc else None, 'chinese': chinese})
        result.setdefault(key, []).append({'observation_id':ident,'documents':linked,
            'chinese_source_types':sorted(kinds), 'binding_matches':binding,
            'cache_valid':all(d['cache_valid'] for d in linked),
            'reusable': 'source-extraction-and-identity-only' if binding and all(d['cache_valid'] for d in linked) else 'historical-research-only',
            'admission_eligible':False, 'requires_review':True,
            'gap':None if binding else 'missing-or-changed-english-identity-use-binding'})
    return result

def admission_documents(c):
    docs = {i: json.loads(p) for i,p in c.execute('SELECT id,payload FROM provenance_documents')}
    referenced = set()
    for _, row in load_rows(c):
        referenced.update(x for x in (row.get('document_id'), row.get('source_english_document_id'),
                                     row.get('oracle', {}).get('document_id')) if x)
    require(referenced <= docs.keys(), 'Admission document missing')
    # Only the explicitly marked, never-consumed v1.2 research documents are
    # optional. Unknown/legacy documents retain the original strict boundary.
    return {i: d for i,d in docs.items() if i in referenced or d.get('schema') != 'v1.2-round1-audit-only-source'}


def guard(c):
    for doc in admission_documents(c).values():
        require(fingerprint(Path(doc['cache_path'])) == doc['fingerprint'], 'Provenance evidence cache changed')

def observation_states(c):
    result={}
    for payload, in c.execute('SELECT payload FROM provenance_observations ORDER BY rowid'):
        event=json.loads(payload)
        if event.get('document_id'):
            result[event['document_id']]={k:event.get(k) for k in ('status','sha256')}
    return result

def evidence_fingerprint(c):
    # Admission-affecting availability is an input; repeated timestamps are not.
    docs = admission_documents(c)
    data={'provenance_documents': sorted(docs),
          'provenance_translations': list(c.execute('SELECT id FROM provenance_translations ORDER BY id'))}
    data['availability']={i:v for i,v in observation_states(c).items() if i in docs}
    return hash_text(canonical(data))

def context_fingerprint(context):
    return hash_text(canonical({
        'english': context.get('english', ''),
        'field_use': context.get('field_use', []),
        'identity': context.get('identity', {}),
    }))

def cached_selection(c, translation_id, context, evidence_hash):
    row = c.execute(
        'SELECT payload FROM provenance_selection_cache '
        'WHERE translation_id=? AND context_hash=? AND evidence_hash=?',
        (translation_id, context_fingerprint(context), evidence_hash)).fetchone()
    return json.loads(row[0]) if row else None

def cache_selection(c, translation_id, context, evidence_hash, value):
    ident = hash_text(canonical({
        'translation_id': translation_id,
        'context_hash': context_fingerprint(context),
        'evidence_hash': evidence_hash,
        'value': value,
    }))
    c.execute(
        'INSERT OR IGNORE INTO provenance_selection_cache '
        '(id,translation_id,context_hash,evidence_hash,payload) VALUES (?,?,?,?,?)',
        (ident, translation_id, context_fingerprint(context), evidence_hash, canonical(value)))
    return ident

def changes(before, after):
    # Word-aware alignment with exact character offsets avoids fragments like "f any o".
    left=list(re.finditer(r'\S+|\s+',before)); right=list(re.finditer(r'\S+|\s+',after))
    result=[]
    for tag,a,b,x,y in difflib.SequenceMatcher(a=[m[0] for m in left],b=[m[0] for m in right],autojunk=False).get_opcodes():
        if tag=='equal':continue
        start=left[a].start() if a<len(left) else len(before)
        end=left[b-1].end() if b>a else start
        new_start=right[x].start() if x<len(right) else len(after)
        new_end=right[y-1].end() if y>x else new_start
        result.append({'before_range':[start,end],'after_range':[new_start,new_end],
                       'before':before[start:end],'after':after[new_start:new_end]})
    return result

def layout(text):
    # Only whitespace and typographic quotes, NOT deletion of punctuation, numbers, signs, or words.
    return re.sub(r'\s+', ' ', text.translate(str.maketrans({'’':"'",'“':'"','”':'"'}))).strip()

def classify(before, current, oracle=None, clarification=False):
    delta = changes(before, current)
    if before == current:
        kind = 'unchanged'
    elif layout(before) == layout(current):
        kind = 'layout-only'
    elif clarification:
        kind = 'official-template-clarification'
    else:
        kind = 'possible-semantic-change'
    return {'classification': kind, 'changes': delta, 'oracle_changes': changes(current, oracle) if oracle else [],
            'semantic_equivalence_automatically_approved': False}

def contexts(official):
    """Exact Arena identities, including every shared LocId consumer. Never match by name alone."""
    from registry import roles
    result = {}
    with database(official['card']) as db:
        use = roles(db); consumers = {}
        db.row_factory = sqlite3.Row
        abilities = {r['Id']: dict(r) for r in db.execute('SELECT * FROM Abilities')}
        def ability_refs(value, seen):
            refs = set()
            for token in (value or '').split(','):
                bits = token.split(':')
                if not bits[0].isdigit(): continue
                aid = int(bits[0])
                if len(bits) == 2 and bits[1].isdigit(): refs.add(int(bits[1]))
                if aid in seen: continue
                seen.add(aid)
                ability = abilities.get(aid, {})
                if ability.get('TextId'): refs.add(ability['TextId'])
                for field in ('EmbeddedActiveAbilityIds','HiddenAbilityIds','ReferencedAbilityIds','ModalChildIds'):
                    refs.update(ability_refs(ability.get(field), seen))
            return refs
        for r in db.execute('SELECT * FROM Cards'):
            identity = {k: r[k] for k in ('GrpId','TitleId','ExpansionCode','CollectorNumber','IsToken','IsPrimaryCard',
                         'IsDigitalOnly','IsRebalanced','LinkedFaceType','LinkedFaceGrpIds')}
            refs = [r[k] for k in ('TitleId','AltTitleId','InterchangeableTitleId','FlavorTextId','ReminderTextId','TypeTextId','SubtypeTextId') if r[k]]
            for field in ('AbilityIds','HiddenAbilityIds'):
                refs += list(ability_refs(r[field], set()))
            for key in set(refs):
                consumers.setdefault(str(key), []).append(identity)
        for key,en in db.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1'):
            result['card',str(key)] = {'english':en or '', 'field_use':use.get(str(key),use.get(key,[])),
                                      'identity': {'cards': sorted(consumers.get(str(key),[]), key=lambda r:r['GrpId'])}}
    with database(official['client']) as db:
        for key,en in db.execute('SELECT Key,enUS FROM Loc'):
            result['client',str(key)] = {'english':en or '', 'field_use':['Loc.enUS'], 'identity':{'client_key':str(key)}}
    return result

def evaluate(row, context, document, oracle_document=None, conflicts=False, unavailable=False):
    en = context['english']
    oracle = row.get('oracle')
    clarification = bool(oracle and oracle.get('classification') == 'template-clarification'
                         and oracle.get('client_english') == en and oracle_document
                         and oracle_document['source_type'] == 'official')
    result = classify(row['source_english'],en,oracle['text'] if oracle else None,clarification)
    result.update(status='needs-review', requires_review=True, translation=None)
    reason = None
    if unavailable: reason = 'source-unavailable-or-changed'
    elif row['identity'] != context['identity']: reason = 'identity-or-face-mismatch'
    elif not context['field_use'] or row['field_use'] != context['field_use']: reason = 'field-use-mismatch'
    elif row['language'] != 'zh-Hans': reason = 'language-variant-review'
    elif conflicts: reason = 'conflicting-source-translations'
    elif safe_format(en,row['translation']): reason = 'format-unsafe'
    elif any(x.get('IsDigitalOnly') or x.get('IsRebalanced') for x in context['identity'].get('cards',[])):
        reason = 'digital-client-authority-review'
    elif oracle and (oracle.get('client_english') != en or layout(oracle['text']) != layout(en)): reason = 'client-oracle-version-mismatch'
    elif document['source_type'] != 'official': reason = 'nonofficial-candidate'
    elif result['classification'] not in {'unchanged','layout-only'}: reason = 'semantic-review-required'
    else:
        result.update(status='verified',requires_review=False,translation=row['translation'])
        reason = 'verified-official-source-exact-version-identity-format'
    result['reason'] = reason
    return result

def refresh_documents(c, document_ids, max_requests=2):
    """Bounded official reads. Failures/staleness are observations, never approval or state erasure."""
    bad, attempts = set(), []
    for ident in sorted(set(document_ids)):
        doc = json.loads(c.execute('SELECT payload FROM provenance_documents WHERE id=?',(ident,)).fetchone()[0])
        # PDFs are historical immutable references; refresh only the configured current Oracle endpoint.
        if doc.get('role') != 'oracle-current': continue
        prior = [json.loads(p) for p, in c.execute('SELECT payload FROM provenance_observations')]
        prior = [p for p in prior if p.get('document_id') == ident and p.get('day') == now()[:10]]
        if prior and prior[-1]['status'] == 'unchanged':
            last = prior[-1]; attempts.append(last)
            if last['status'] != 'unchanged': bad.add(ident)
            continue
        event = {'document_id':ident,'at':now(),'day':now()[:10], 'status':'budget-exhausted'}
        if len(attempts) < max_requests:
            try:
                require(official_url(doc['url']), 'Unapproved network host')
                request = urllib.request.Request(doc['url'],headers={'User-Agent':'MTGA-CN-Maintainer/1.1'})
                with urllib.request.urlopen(request,timeout=15) as response:
                    require(official_url(response.url), 'Unapproved redirect')
                    data=response.read(8*1024*1024+1)
                require(len(data)<=8*1024*1024,'Source too large')
                event['sha256']=hashlib.sha256(data).hexdigest()
                event['status']='unchanged' if event['sha256']==doc['fingerprint']['sha256'] else 'changed-needs-source-review'
                target=managed_path(doc['cache_path']).parent/event['sha256']
                if not target.exists(): atomic_bytes(target,data)
                event['snapshot']=str(target)
            except (OSError,RuntimeError,ValueError) as error:
                event.update(status='unavailable',error=str(error))
        if event['status']!='unchanged': bad.add(ident)
        append(c,'provenance_observations',event);attempts.append(event)
    c.commit()
    return bad,attempts

def audit(c, official, version, run_id, limit=20, refresh=False, activate=False):
    require(1 <= limit <= 100, 'Audit limit must be 1..100')
    guard(c)
    ctx=contexts(official); rows=load_rows(c)
    from registry import bind_contexts
    bind_contexts(c, ctx)
    docs={i:json.loads(p) for i,p in c.execute('SELECT id,payload FROM provenance_documents')}
    prior_english={(layer,key):en for layer,key,en in c.execute('SELECT layer,key,english FROM decisions WHERE run_id=?',(run_id,))}
    current_changes=[{'layer':layer,'key':key,'changes':changes(en or '',ctx[layer,key]['english']),
                     'classification':classify(en or '',ctx[layer,key]['english'])['classification']}
                    for (layer,key),en in prior_english.items() if (layer,key) in ctx and en != ctx[layer,key]['english']]
    pending=list(c.execute("SELECT layer,key,status,english,reason FROM decisions WHERE run_id=? AND status IN ('candidate','needs-review') ORDER BY layer,key",(run_id,)))
    # Audit all registered evidence locally; only bounded targets can trigger network reads.
    selected=sorted(rows,key=lambda item:(item[1].get('priority',9),item[1]['layer'],str(item[1]['key'])))[:limit]
    ids=[r['oracle']['document_id'] for _,r in selected if r.get('oracle')]
    _,network=refresh_documents(c,ids) if refresh else (set(),[])
    # A partial refresh must never erase failures for other registered documents.
    bad={i for i,e in observation_states(c).items() if e['status']!='unchanged'}
    selection_evidence_hash = evidence_fingerprint(c)
    c.execute('CREATE TEMP TABLE IF NOT EXISTS provenance_selection(layer TEXT,key TEXT,english TEXT,field_use TEXT,translation TEXT,source TEXT,status TEXT,reason TEXT,PRIMARY KEY(layer,key))')
    c.execute('DELETE FROM provenance_selection')
    evaluations=[]
    choices={}
    for ident,row in rows:
        key=(row['layer'],str(row['key'])); context=ctx.get(key)
        if context is None: context={'english':'','field_use':[],'identity':{}}
        related=[(i,x) for i,x in rows if (x['layer'],str(x['key']))==key and x['language']==row['language']]
        applicable=[(i,x) for i,x in related if x['identity']==context['identity'] and x['field_use']==context['field_use']
                    and (layout(x['source_english'])==layout(context['english']) or
                         (x.get('oracle',{}).get('client_english')==context['english'] and
                          layout(x.get('oracle',{}).get('text',''))==layout(context['english'])))]
        od=docs.get(row.get('oracle',{}).get('document_id'))
        unavailable=row['document_id'] in bad or row.get('oracle',{}).get('document_id') in bad
        cached = cached_selection(c, ident, context, selection_evidence_hash)
        if cached:
            result = cached['result']
            evaluations.append({'id':cached['event_id'],'layer':key[0],'key':key[1],
                                'reused_review':True,'reused_selection':True,**result})
            choices.setdefault(key,[]).append((context,result,ident))
            continue
        # Evaluate eligibility first. Nonadmitted candidates have no global veto.
        result=evaluate(row,context,docs[row['document_id']],od,False,unavailable)
        result['other_version_or_identity_evidence']=[i for i,x in related if i not in {a for a,_ in applicable}]
        event={'translation_id':ident,'client_version':version,'english_hash':hash_text(context['english']),
               'oracle_hash':row.get('oracle',{}).get('hash'), 'context_hash':hash_text(canonical(context)),
               'review_kind':'automatic-source-check','reviewer':'deterministic-v1.1','basis':result,
               'last_verified_version':version if result['status']=='verified' else None}
        prior=[(i,json.loads(p)) for i,p in c.execute('SELECT id,payload FROM provenance_reviews WHERE translation_id=?',(ident,))]
        reused=next(((i,e) for i,e in prior if {k:v for k,v in e.items() if k!='reviewed_at'}==event),None)
        if reused:
            event_id=reused[0]
        else:
            event['reviewed_at']=now()
            event_id=append(c,'provenance_reviews',event,translation_id=ident)
        cache_selection(c, ident, context, selection_evidence_hash,
                        {'event_id':event_id,'result':result})
        evaluations.append({'id':event_id,'layer':key[0],'key':key[1],
                            'reused_review':bool(reused),'reused_selection':False,**result})
        choices.setdefault(key,[]).append((context,result,ident))
    if activate:
        for key,options in choices.items():
            # Valid official evidence can coexist with an identical community candidate.
            options.sort(key=lambda x:(x[1]['status']!='verified',x[2]))
            context,result,ident=options[0]
            eligible = [r for _,r,_ in options if r['status']=='verified']
            if len({r['translation'] for r in eligible}) > 1:
                result = {**result, 'status':'needs-review', 'translation':None,
                          'reason':'conflicting-applicable-official-translations'}
            c.execute('INSERT INTO provenance_selection VALUES (?,?,?,?,?,?,?,?)',
                      (*key,context['english'],canonical(context['field_use']),result['translation'],'provenance:'+ident,result['status'],result['reason']))
    index = evidence_index(c, ctx)
    categories={'official_chinese':0,'community_chinese':0,'local_chinese_only':0,'no_registered_chinese':0,'unresearched_external':0,'donor_chinese_only':0,'other_local_chinese_only':0}
    detailed=[]
    for layer,key,status,en,reason in pending:
        evidence=[r for _,r in rows if r['layer']==layer and str(r['key'])==key]
        research = index.get((layer,key), [])
        types={docs[r['document_id']]['source_type'] for r in evidence}
        types.update(kind for item in research for kind in item['chinese_source_types'])
        local=[(s,zh) for s,zh in c.execute('SELECT source_id,chinese FROM localization WHERE layer=? AND key=?',(layer,key)) if has_chinese(zh)]
        flags={'official_chinese':'official' in types,'community_chinese':'community' in types,
               'local_chinese_only':bool(local) and not (types & {'official','community'}),
               'no_registered_chinese':not local and not evidence and not types,'unresearched_external':not evidence and not research,
               'donor_chinese_only':bool(local) and not(types & {'official','community'}) and any('MTGA Mod(MacOS)' in source for source,_ in local),
               'other_local_chinese_only':bool(local) and not(types & {'official','community'}) and not any('MTGA Mod(MacOS)' in source for source,_ in local)}
        for k,v in flags.items(): categories[k]+=int(v)
        priority=1 if flags['official_chinese'] else (2 if local and 'english-changed' in reason else (3 if layer=='card' and 'unknown-field-use' not in reason else (4 if flags['community_chinese'] else 5)))
        detailed.append({'priority':priority,'layer':layer,'key':key,'status':status,'reason':reason,'sources':[s for s,_ in local],
                         'research_evidence':research, **flags})
    c.commit()
    return {'schema':'MTGA_SOURCE_AUDIT_V1.1','run_id':run_id,'version':version,'at':now(),
            'pending_total':len(pending),'needs_review':sum(x[2]=='needs-review' for x in pending),
            'candidate':sum(x[2]=='candidate' for x in pending),'categories':categories,
            'scope':'Existing latest-run candidate + needs-review records; external absence is unknown, not proven nonexistent.',
            'network':network,'registered_evaluations':evaluations,'queue':sorted(detailed,key=lambda r:(r['priority'],r['layer'],r['key'])),
            'bounded_source_worklist':sorted(detailed,key=lambda r:(r['priority'],r['layer'],r['key']))[:limit],
            'current_client_english_changes':current_changes,
            'existing_english_changed_pending':sum('english-changed' in r[4] for r in pending),
            'semantic_review_records':len({(e['layer'],e['key']) for e in evaluations if e['classification'] in {'official-template-clarification','possible-semantic-change'}}),
            'semantic_rule_change_confirmed':0,'network_budget':2,'source_target_limit':limit,
            'automatic_checks_are_not_human_approval':True,'candidate_rebuilt':False}

def select(c,layer,key,en,use):
    exists=c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='provenance_selection'").fetchone()
    if not exists:return None
    row=c.execute('SELECT english,field_use,translation,source,status,reason FROM provenance_selection WHERE layer=? AND key=?',(layer,str(key))).fetchone()
    if not row or row[0]!=en or row[1]!=canonical(use):return None
    return row[2:]

def write_audit(path,report):
    path=Path(path);path.mkdir(parents=True,exist_ok=True)
    save_json(path/'report.json',report)
    lines=['# MTGA Maintainer v1.1 来源审核','', '阅读顺序：待审范围 → 已核验来源 → 规则差异与缺口。', '',
           f'待处理 {report["pending_total"]}：needs-review {report["needs_review"]}，candidate {report["candidate"]}。',
           '', '计数按 (layer, Key)；来源维度可重叠。未联网核验不能解释为官方从未翻译。','']
    lines += [f'- {k}: {v}' for k,v in report['categories'].items()]
    lines += ['',f'已登记来源的本地规则核查：{len(report["registered_evaluations"])} 条。',
              f'规则/措辞差异需复核：{report["semantic_review_records"]}；自动确认实质规则变化：0。','']
    for e in report['registered_evaluations']:
        lines.append(f'- {e["layer"]}/{e["key"]}: {e["status"]}; {e["classification"]}; {e["reason"]}')
    lines += ['', '没有修改游戏、重建候选或提升人工审核状态。网络缺口与逐项差异见 report.json。','']
    atomic_bytes(path/'REPORT.md','\n'.join(lines).encode())
