import copy,datetime as dt,json,sys,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path

ROOT=Path(__file__).parent
CFG=json.loads((ROOT/'resolver_input.json').read_text())
API='https://api.modrinth.com/v2'
GAME,LOADER=CFG['game'],CFG['loader']
HEAD={'User-Agent':'ATM11-Cozy-Resolver/1.0 (github.com/RexiRexii/testing)','Accept':'application/json'}

class E(RuntimeError): pass

def call(path,method='GET',body=None):
    data=None; h=dict(HEAD)
    if body is not None:
        data=json.dumps(body,separators=(',',':')).encode(); h['Content-Type']='application/json'
    url=API+path
    for n in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,data=data,headers=h,method=method),timeout=45) as r:
                b=r.read(); return json.loads(b) if b else None
        except urllib.error.HTTPError as x:
            text=x.read().decode('utf-8','replace')
            if x.code in (429,500,502,503,504) and n<4: time.sleep(min(2**(n+1),16)); continue
            raise E(f'HTTP {x.code} {url}: {text[:300]}')
        except Exception as x:
            if n<4: time.sleep(min(2**(n+1),16)); continue
            raise E(f'{url}: {x}')

def getproj(x):
    k=x.lower()
    if k not in PC:
        p=call('/project/'+urllib.parse.quote(x,safe=''))
        for y in (x,p['id'],p['slug']): PC[y.lower()]=p
    return PC[k]

def getver(x):
    if x not in VC: VC[x]=call('/version/'+x)
    return VC[x]

def versions(p):
    q=urllib.parse.urlencode({'loaders':json.dumps([LOADER]),'game_versions':json.dumps([GAME]),'include_changelog':'false'})
    a=call(f"/project/{p['id']}/version?{q}")
    a=[v for v in a if GAME in v.get('game_versions',[]) and LOADER in v.get('loaders',[])]
    for v in a: VC[v['id']]=v
    rank={'release':3,'beta':2,'alpha':1}
    return sorted(a,key=lambda v:(rank.get(v.get('version_type'),0),v.get('date_published','')),reverse=True)

def dep_pid(d):
    return d.get('project_id') or (getver(d['version_id'])['project_id'] if d.get('version_id') else None)

def env(v):
    e=v.get('environment','unknown')
    return {
      'client_only':{'client':'required','server':'unsupported'},
      'client_only_server_optional':{'client':'required','server':'optional'},
      'singleplayer_only':{'client':'required','server':'unsupported'},
      'server_only':{'client':'unsupported','server':'required'},
      'server_only_client_optional':{'client':'optional','server':'required'},
      'dedicated_server_only':{'client':'unsupported','server':'required'}
    }.get(e,{'client':'required','server':'required'})

def file(v):
    fs=v.get('files',[]); fs=[f for f in fs if f.get('primary')] or fs
    fs=[f for f in fs if f.get('filename','').lower().endswith('.jar') and f.get('file_type') not in ('sources-jar','dev-jar','javadoc-jar','signature')] or fs
    if not fs: raise E(f"No usable file for {v['id']}")
    return fs[0]

PC={}; VC={}; selected={}; requested=set(); notes=[]
existing={p:set() for p,_ in CFG['indexed']}
for p,v in CFG['indexed']: existing.setdefault(p,set()).add(v)
# Hash-match bundled ATM11 JARs so existing dependencies are reused.
for i in range(0,len(CFG['overrides']),100):
    m=call('/version_files','POST',{'hashes':CFG['overrides'][i:i+100],'algorithm':'sha1'}) or {}
    for v in m.values(): VC[v['id']]=v; existing.setdefault(v['project_id'],set()).add(v['id'])

def resolve(identifier,exact=None,stack=()):
    p=getproj(getver(exact)['project_id'] if exact else identifier); pid=p['id']
    if pid in stack: return
    if pid in existing:
        if exact and exact not in existing[pid]: raise E(f"{p['title']} requires {exact}, ATM11 has {sorted(existing[pid])}")
        notes.append(p['title']); return
    if pid in selected:
        if exact and selected[pid]['id']!=exact: raise E(f"Conflicting exact versions for {p['title']}")
        return
    cand=[getver(exact)] if exact else versions(p)
    if not cand: raise E(f"No {GAME}/{LOADER} version for {p['title']}")
    base=copy.deepcopy(selected); errs=[]
    for v in cand:
        selected.clear(); selected.update(copy.deepcopy(base))
        try:
            if GAME not in v.get('game_versions',[]) or LOADER not in v.get('loaders',[]): raise E('wrong game/loader tags')
            for d in v.get('dependencies',[]):
                if d.get('dependency_type')=='incompatible' and dep_pid(d) in (set(existing)|set(selected)): raise E(f"declares {dep_pid(d)} incompatible")
            selected[pid]=v
            for d in v.get('dependencies',[]):
                if d.get('dependency_type')=='required':
                    q=dep_pid(d)
                    if not q: raise E('required external dependency has no Modrinth project')
                    resolve(q,d.get('version_id'),stack+(pid,))
            return
        except E as x: errs.append(f"{v.get('version_number',v['id'])}: {x}")
    selected.clear(); selected.update(base)
    raise E(f"No usable version for {p['title']}: {' | '.join(errs[:6])}")

fails=[]
for slug in CFG['requested']:
    try:
        p=getproj(slug); requested.add(p['id']); resolve(p['id'])
    except Exception as x: fails.append({'slug':slug,'error':str(x)})
if fails:
    (ROOT/'resolved-mods.json').write_text(json.dumps({'ok':False,'failures':fails},indent=2)); raise E(str(fails))

entries=[]; projects=[]
for pid,v in sorted(selected.items(),key=lambda x:getproj(x[0])['title'].lower()):
    p=getproj(pid); f=file(v); h=f.get('hashes',{})
    if not h.get('sha1') or not h.get('sha512'): raise E(f"Missing hashes for {f.get('filename')}")
    path='mods/'+f['filename']
    entries.append({'path':path,'hashes':{'sha1':h['sha1'],'sha512':h['sha512']},'env':env(v),'downloads':[f['url']],'fileSize':f['size']})
    projects.append({'project_id':pid,'slug':p['slug'],'title':p['title'],'version_id':v['id'],'version_number':v['version_number'],'requested':pid in requested,'path':path,'url':f['url']})
out={'ok':True,'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'game_version':GAME,'loader':LOADER,'entries':entries,'selected_projects':projects,'reused_baseline_dependencies':sorted(set(notes))}
(ROOT/'resolved-mods.json').write_text(json.dumps(out,indent=2))
lines=['# ATM11 Cozy Expansion resolution','',f'- Minecraft `{GAME}` / `{LOADER}`',f'- Requested: `{len(requested)}`; new files including dependencies: `{len(entries)}`','','| Type | Project | Version | File |','|---|---|---|---|']
for x in projects: lines.append(f"| {'Requested' if x['requested'] else 'Dependency'} | {x['title']} | `{x['version_number']}` | `{x['path']}` |")
if notes: lines+=['','## Reused baseline dependencies','']+[f'- {x}' for x in sorted(set(notes))]
(ROOT/'resolution-report.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({'ok':True,'requested':len(requested),'files':len(entries)}))
