import json, os, time, threading, urllib.parse
from pathlib import Path
import requests

DATA=Path('/data')
TOKEN=DATA/'google-token.json'
ID=Path('/run/secrets/google-client-id')
SECRET=Path('/run/secrets/google-client-secret')
CALLBACK='https://leech.cs2.cat/oauth/google/callback'
SCOPE='https://www.googleapis.com/auth/drive.file'
LOCK=threading.RLock()

def configured(): return ID.is_file() and SECRET.is_file()
def connected(): return TOKEN.is_file()
def save_token(data):
    tmp=DATA/'google-token.new'
    with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f: json.dump(data,f)
    os.chmod(tmp,0o600); os.replace(tmp,TOKEN)
def auth_url(state):
    if not configured(): raise ValueError('Google client is not configured')
    p={'client_id':ID.read_text().strip(),'redirect_uri':CALLBACK,'response_type':'code','scope':SCOPE,'access_type':'offline','prompt':'consent','state':state}
    return 'https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode(p)
def exchange(code):
    with LOCK:
        r=requests.post('https://oauth2.googleapis.com/token',data={'code':code,'client_id':ID.read_text().strip(),'client_secret':SECRET.read_text().strip(),'redirect_uri':CALLBACK,'grant_type':'authorization_code'},timeout=30)
        if not r.ok: raise ValueError('Google authorization failed')
        data=r.json(); data['expires_at']=time.time()+data.get('expires_in',3600)-60
        if not data.get('refresh_token'): raise ValueError('Google did not provide an offline token')
        save_token(data)
def access_token():
    with LOCK:
        if not connected(): raise ValueError('Google Drive is not connected')
        data=json.loads(TOKEN.read_text())
        if time.time()<data.get('expires_at',0): return data['access_token']
        r=requests.post('https://oauth2.googleapis.com/token',data={'refresh_token':data['refresh_token'],'client_id':ID.read_text().strip(),'client_secret':SECRET.read_text().strip(),'grant_type':'refresh_token'},timeout=30)
        if not r.ok: raise ValueError('Google Drive authorization expired; reconnect the account')
        fresh=r.json(); data.update(fresh); data['expires_at']=time.time()+fresh.get('expires_in',3600)-60; save_token(data)
        return data['access_token']
def api(method,url,**kw):
    headers=kw.pop('headers',{}); headers['Authorization']='Bearer '+access_token()
    r=requests.request(method,url,headers=headers,timeout=120,**kw)
    if not r.ok: raise ValueError('Google Drive error (HTTP '+str(r.status_code)+')')
    return r.json() if r.content else {}
def upload(name,size,reader,progress,parent_id=None):
    if size is None or size<0: raise ValueError('Google Drive requires a known file size')
    token=access_token()
    metadata={'name':name}
    if parent_id: metadata['parents']=[parent_id]
    r=requests.post('https://www.googleapis.com/upload/drive/v3/files',params={'uploadType':'resumable','fields':'id,name,webViewLink'},headers={'Authorization':'Bearer '+token,'X-Upload-Content-Type':'application/octet-stream','X-Upload-Content-Length':str(size),'Content-Type':'application/json'},json=metadata,timeout=30)
    if not r.ok or not r.headers.get('Location'): raise ValueError('Could not start Google Drive upload')
    url=r.headers['Location']; done=0; chunk_size=8*1024*1024
    if size==0:
        r=requests.put(url,data=b'',headers={'Content-Length':'0','Content-Range':'bytes */0'},timeout=120)
        if not r.ok: raise ValueError('Could not upload empty file to Google Drive')
        return r.json()
    while done<size:
        wanted=min(chunk_size,size-done); buf=bytearray()
        while len(buf)<wanted:
            part=reader.read(wanted-len(buf))
            if not part: raise ValueError('Incomplete source stream')
            buf.extend(part)
        end=done+len(buf)-1
        r=requests.put(url,data=bytes(buf),headers={'Content-Length':str(len(buf)),'Content-Range':f'bytes {done}-{end}/{size}'},timeout=120)
        if r.status_code not in (200,201,308): raise ValueError('Google Drive upload failed (HTTP '+str(r.status_code)+')')
        done=end+1; progress(done,size)
    if r.status_code not in (200,201): raise ValueError('Google Drive upload not finalized')
    return r.json()
def share(id):
    return api('POST','https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(id,safe='')+'/permissions',json={'type':'anyone','role':'reader'})
def remove(id):
    api('DELETE','https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(id,safe=''))

MAP=DATA/'google-folders.json'
def _mapping():
    if not MAP.exists(): return {}
    return json.loads(MAP.read_text())
def _save_mapping(data):
    tmp=DATA/'google-folders.new'
    with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f: json.dump(data,f)
    os.chmod(tmp,0o600); os.replace(tmp,MAP)
def _owner():
    info=api('GET','https://www.googleapis.com/drive/v3/about',params={'fields':'user(permissionId)'})
    owner=info.get('user',{}).get('permissionId')
    if not owner: raise ValueError('Google Drive account identity unavailable')
    return owner
def _new_folder(name,parent=None):
    data={'name':name,'mimeType':'application/vnd.google-apps.folder'}
    if parent: data['parents']=[parent]
    return api('POST','https://www.googleapis.com/drive/v3/files',params={'fields':'id'},json=data)['id']
def _ensure_public(id):
    u='https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(id,safe='')+'/permissions'
    permissions=api('GET',u,params={'fields':'permissions(type,role)'}).get('permissions',[])
    if not any(p.get('type')=='anyone' and p.get('role') in ('reader','writer') for p in permissions): share(id)
def ensure_folder(visibility,folder_id=None,folder_name=None):
    if visibility not in ('private','public'): raise ValueError('Invalid visibility')
    with LOCK:
        owner=_owner(); data=_mapping(); entry=data.setdefault(owner,{'folders':{}})
        if not entry.get('root'):
            entry['root']=_new_folder('LeechBox'); _save_mapping(data)
        if not entry.get(visibility):
            entry[visibility]=_new_folder('Public' if visibility=='public' else 'Private',entry['root']); _save_mapping(data)
        branch=entry[visibility]
        if visibility=='public': _ensure_public(branch)
        if not folder_id: return branch
        if not folder_name: raise ValueError('Folder name is missing')
        local=entry['folders'].setdefault(folder_id,{})
        if not local.get(visibility):
            local[visibility]=_new_folder(folder_name,branch); _save_mapping(data)
        return local[visibility]
def rename_folder(folder_id,new_name,old_name):
    with LOCK:
        owner=_owner(); entry=_mapping().get(owner,{}); ids=list(entry.get('folders',{}).get(folder_id,{}).values()); changed=[]
        try:
            for id in ids:
                api('PATCH','https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(id,safe=''),json={'name':new_name}); changed.append(id)
        except Exception:
            for id in changed:
                try: api('PATCH','https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(id,safe=''),json={'name':old_name})
                except Exception: pass
            raise
def move_file(id,parent_id):
    u='https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(id,safe='')
    existing=api('GET',u,params={'fields':'parents'}).get('parents',[])
    if existing==[parent_id]: return
    api('PATCH',u,params={'addParents':parent_id,'removeParents':','.join(existing),'fields':'id,parents'},json={})
def forget_folder(folder_id):
    with LOCK:
        owner=_owner(); data=_mapping(); entry=data.get(owner,{})
        entry.get('folders',{}).pop(folder_id,None); _save_mapping(data)
def root_links():
    with LOCK:
        entry=_mapping().get(_owner(),{})
        return {k:'https://drive.google.com/drive/folders/'+urllib.parse.quote(entry[k],safe='') for k in ('public','private') if entry.get(k)}
def subfolder_links():
    with LOCK:
        entry=_mapping().get(_owner(),{})
        return {folder_id:{visibility:'https://drive.google.com/drive/folders/'+urllib.parse.quote(remote_id,safe='') for visibility,remote_id in branches.items() if visibility in ('public','private') and remote_id} for folder_id,branches in entry.get('folders',{}).items()}
