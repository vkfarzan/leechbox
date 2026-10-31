import hashlib, hmac, json, os, secrets, sqlite3, threading, time, urllib.parse, shutil, socket, ipaddress, http.client, re, html
from email.message import Message
from pathlib import Path
import requests
import gdrive
from flask import Flask, request, session, redirect, jsonify, send_file, abort, Response

D = Path('/data'); D.mkdir(exist_ok=True)
(D / 'files').mkdir(exist_ok=True)
def secret(name, size=32):
    p=D/name
    if not p.exists():
        p.write_text(secrets.token_urlsafe(size)); p.chmod(0o600)
    return p.read_text().strip()
password=secret('admin-password', 18)
app=Flask(__name__); app.secret_key=secret('session-secret')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE='Lax')
dbpath=D/'jobs.sqlite3'
def db():
    c=sqlite3.connect(dbpath, timeout=30); c.row_factory=sqlite3.Row; return c
with db() as c:
    c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT, filename TEXT, status TEXT, progress INTEGER DEFAULT 0, size INTEGER DEFAULT 0, error TEXT DEFAULT "", created REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE)')
    if 'folder_id' not in [r['name'] for r in c.execute('PRAGMA table_info(jobs)')]:
        c.execute('ALTER TABLE jobs ADD COLUMN folder_id TEXT')
    if 'downloads' not in [r['name'] for r in c.execute('PRAGMA table_info(jobs)')]:
        c.execute('ALTER TABLE jobs ADD COLUMN downloads INTEGER NOT NULL DEFAULT 0')
    for name, definition in [('storage',"TEXT NOT NULL DEFAULT 'server'"),('cloud_id','TEXT'),('visibility',"TEXT NOT NULL DEFAULT 'private'")]:
        if name not in [r['name'] for r in c.execute('PRAGMA table_info(jobs)')]: c.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+definition)
    c.execute("UPDATE jobs SET status='failed', error='Interrupted by service restart' WHERE status IN ('queued','processing','downloading','uploading')")
def update(id, **fields):
    with db() as c: c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(),id))
def rd(method, path, **kw):
    token=Path('/run/secrets/rd-token').read_text().strip()
    r=requests.request(method,'https://api.real-debrid.com/rest/1.0/'+path,headers={'Authorization':'Bearer '+token},timeout=45,**kw)
    if not r.ok:
        try: msg=r.json().get('error','API error')
        except Exception: msg='API error'
        raise ValueError('Real-Debrid: '+str(msg))
    return r.json() if r.content else None
def newjob(src, folder_id=None):
    id=secrets.token_urlsafe(24)
    with db() as c: c.execute('INSERT INTO jobs (id,source,status,created,folder_id) VALUES (?,?,?,?,?)',(id,src,'queued',time.time(),folder_id))
    return id
def host_folder(folder_id,create=False):
    if not folder_id or not re.fullmatch(r'[A-Za-z0-9_-]+',folder_id): raise ValueError('Invalid folder ID')
    p=D/'files'/'folders'/folder_id
    if create: p.mkdir(parents=True,exist_ok=True,mode=0o755)
    return p
def host_path(id,folder_id=None,create=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]+',id): raise ValueError('Invalid file ID')
    return (host_folder(folder_id,create) if folder_id else D/'files')/id
def existing_host_path(id,folder_id=None):
    expected=host_path(id,folder_id)
    if expected.is_file(): return expected
    old=host_path(id)
    return old if old.is_file() else expected
def finish_host(id,tmp):
    with db() as c: row=c.execute('SELECT folder_id FROM jobs WHERE id=?',(id,)).fetchone()
    if not row: raise ValueError('File no longer exists')
    dest=host_path(id,row['folder_id'],create=True)
    tmp.replace(dest)
def download(id, result):
    url=result['download']; name=os.path.basename(result.get('filename') or 'download').replace('\x00','')[:220] or 'download'
    update(id,status='downloading',filename=name)
    tmp=D/'files'/(id+'.part')
    try:
        with requests.get(url,stream=True,timeout=(20,120)) as r:
            r.raise_for_status(); total=int(r.headers.get('Content-Length') or result.get('filesize') or 0); done=0; last=0
            with tmp.open('wb') as f:
                for chunk in r.iter_content(1024*1024):
                    if not chunk: continue
                    f.write(chunk); done+=len(chunk)
                    if time.monotonic()-last>2:
                        update(id,progress=min(99,int(done*100/total)) if total else 0,size=done); last=time.monotonic()
        if total and done != total: raise ValueError('Incomplete download')
        finish_host(id,tmp); update(id,status='done',progress=100,size=done)
    finally:
        tmp.unlink(missing_ok=True)
def save_cloud(id,name,size,reader,visibility):
    name=os.path.basename(name.replace('\\','/')).replace('\x00','')[:220] or 'download'
    update(id,status='uploading',filename=name,storage='google')
    with db() as c:
        job=c.execute('SELECT folder_id FROM jobs WHERE id=?',(id,)).fetchone()
        folder_id=job['folder_id']
        folder=c.execute('SELECT name FROM folders WHERE id=?',(folder_id,)).fetchone() if folder_id else None
        if folder_id and not folder: raise ValueError('The selected folder no longer exists')
        folder_name=folder['name'] if folder else None
    parent=gdrive.ensure_folder(visibility,folder_id,folder_name)
    def progress(done,total): update(id,progress=min(99,int(done*100/total)) if total else 0,size=done)
    result=gdrive.upload(name,size,reader,progress,parent_id=parent); remote_id=result['id']
    update(id,cloud_id=remote_id,status='done',progress=100,size=size,visibility=visibility)
def cloud_debrid(id,result,visibility):
    with requests.get(result['download'],stream=True,timeout=(20,120)) as r:
        r.raise_for_status()
        length=r.headers.get('Content-Length') or result.get('filesize')
        if length is None: raise ValueError('Google Drive requires a known file size')
        save_cloud(id,result.get('filename') or 'download',int(length),r.raw,visibility)
def cloud_direct(id,url,visibility):
    conn=None
    try:
        conn,resp,final_url=open_public_url(url)
        msg=Message(); msg['content-disposition']=resp.getheader('Content-Disposition','')
        name=msg.get_filename() or urllib.parse.unquote(urllib.parse.urlsplit(final_url).path.rsplit('/',1)[-1]) or 'download'
        length=resp.getheader('Content-Length')
        if length is None: raise ValueError('Google Drive requires a known file size')
        save_cloud(id,name,int(length),resp,visibility)
    finally:
        if conn: conn.close()
def open_public_url(url):
    for _ in range(6):
        u=urllib.parse.urlsplit(url)
        if u.scheme not in ('http','https') or not u.hostname or u.username is not None or u.password is not None: raise ValueError('Invalid direct URL')
        port=u.port or (443 if u.scheme=='https' else 80)
        if port not in (80,443): raise ValueError('Only ports 80 and 443 are allowed')
        ips=socket.getaddrinfo(u.hostname,port,type=socket.SOCK_STREAM)
        if not ips or any(not ipaddress.ip_address(x[4][0]).is_global for x in ips): raise ValueError('Only public internet addresses are allowed')
        address=ips[0][4][0]
        conn=(http.client.HTTPSConnection if u.scheme=='https' else http.client.HTTPConnection)(u.hostname,port,timeout=120)
        conn._create_connection=lambda ignored,timeout,source_address=None: socket.create_connection((address,port),timeout,source_address)
        path=urllib.parse.urlunsplit(('', '', u.path or '/',u.query,''))
        conn.request('GET',path,headers={'User-Agent':'LeechBox/1.0','Accept-Encoding':'identity'})
        resp=conn.getresponse()
        if resp.status in (301,302,303,307,308):
            location=resp.getheader('Location'); conn.close()
            if not location: raise ValueError('Redirect has no destination')
            url=urllib.parse.urljoin(url,location); continue
        if resp.status!=200:
            code=resp.status; conn.close(); raise ValueError('Source returned HTTP '+str(code))
        return conn,resp,url
    raise ValueError('Too many redirects')
def download_direct(id,url):
    conn=None; tmp=D/'files'/(id+'.part')
    try:
        conn,resp,final_url=open_public_url(url)
        raw=resp.getheader('Content-Disposition',''); msg=Message(); msg['content-disposition']=raw
        name=msg.get_filename() or urllib.parse.unquote(urllib.parse.urlsplit(final_url).path.rsplit('/',1)[-1]) or 'download'
        name=os.path.basename(name.replace('\\','/')).replace('\x00','')[:220] or 'download'
        total=int(resp.getheader('Content-Length') or 0); done=0; last=0
        if total and total>shutil.disk_usage(D).free-1024**3: raise ValueError('Not enough free space')
        update(id,status='downloading',filename=name)
        with tmp.open('wb') as f:
            while True:
                chunk=resp.read(1024*1024)
                if not chunk: break
                if shutil.disk_usage(D).free < 1024**3: raise ValueError('Not enough free space')
                f.write(chunk); done+=len(chunk)
                if time.monotonic()-last>2:
                    update(id,progress=min(99,int(done*100/total)) if total else 0,size=done); last=time.monotonic()
        if total and done!=total: raise ValueError('Incomplete download')
        finish_host(id,tmp); update(id,status='done',progress=100,size=done)
    finally:
        if conn: conn.close()
        tmp.unlink(missing_ok=True)
def work(id, link, folder_id=None, mode='debrid', storage='server', visibility='private'):
    try:
        update(id,status='processing')
        if mode=='direct':
            if storage=='google': cloud_direct(id,link,visibility)
            else: download_direct(id,link)
        elif link.startswith('magnet:?'):
            torrent=rd('POST','torrents/addMagnet',data={'magnet':link})['id']
            selected=False
            while True:
                info=rd('GET','torrents/info/'+torrent); state=info['status']
                if state=='waiting_files_selection' and not selected:
                    rd('POST','torrents/selectFiles/'+torrent,data={'files':'all'}); selected=True
                if state=='downloaded': break
                if state in ('error','virus','dead','magnet_error'): raise ValueError('Torrent: '+state)
                update(id,progress=int(info.get('progress') or 0)); time.sleep(10)
            links=info.get('links',[])
            if not links: raise ValueError('Torrent has no downloadable files')
            for i, item in enumerate(links):
                job=id if i==0 else newjob(link,folder_id)
                try:
                    result=rd('POST','unrestrict/link',data={'link':item})
                    if storage=='google': cloud_debrid(job,result,visibility)
                    else: download(job,result)
                except Exception as e: update(job,status='failed',error=str(e)[:300])
        else:
            result=rd('POST','unrestrict/link',data={'link':link})
            if isinstance(result,list):
                if not result: raise ValueError('No downloadable files found')
                for i, item in enumerate(result):
                    job=id if i==0 else newjob(link,folder_id)
                    try:
                        if storage=='google': cloud_debrid(job,item,visibility)
                        else: download(job,item)
                    except Exception as e: update(job,status='failed',error=str(e)[:300])
            elif storage=='google': cloud_debrid(id,result,visibility)
            else: download(id,result)
    except Exception as e: update(id,status='failed',error=str(e)[:300])
def is_admin(): return session.get('admin') is True
@app.before_request
def protect():
    if request.method=='POST' and request.endpoint != 'login':
        valid_origin=request.headers.get('Origin') == 'https://leech.cs2.cat'
        valid_token=bool(session.get('csrf')) and hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf'])
        valid_logout_token=(request.endpoint=='logout' and bool(session.get('csrf')) and hmac.compare_digest(request.form.get('csrf',''),session['csrf']))
        if not valid_origin and not valid_token and not valid_logout_token: abort(403)
@app.after_request
def headers(r):
    r.headers['X-Content-Type-Options']='nosniff'; r.headers['Referrer-Policy']='no-referrer'; r.headers['Cache-Control']='no-store'; return r
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        if hmac.compare_digest(request.form.get('password',''),password): session.clear(); session['admin']=True; return redirect('/')
        time.sleep(1); return redirect('/login?error=1')
    return Response(Path('/app/login.html').read_text(encoding='utf-8'),mimetype='text/html')
@app.route('/')
def index():
    if not is_admin(): return redirect('/login')
    token=session.setdefault('csrf',secrets.token_urlsafe(32))
    return Response(Path('/app/dashboard.html').read_text(encoding='utf-8').replace('__CSRF_TOKEN__',token),mimetype='text/html')
@app.route('/connect/google')
def google_connect():
    if not is_admin(): return redirect('/login')
    if not gdrive.configured(): return 'Google Drive configuration missing',503
    state=secrets.token_urlsafe(32); session['google_oauth_state']=state
    return redirect(gdrive.auth_url(state))
@app.route('/oauth/google/callback')
def google_callback():
    if not is_admin(): return redirect('/login')
    expected=session.pop('google_oauth_state',None); actual=request.args.get('state','')
    if not expected or not hmac.compare_digest(expected,actual): abort(403)
    if request.args.get('error') or not request.args.get('code'): return redirect('/?google=cancelled')
    try: gdrive.exchange(request.args['code'])
    except Exception: return redirect('/?google=failed')
    return redirect('/?google=connected')
@app.route('/api/cloud-status')
def cloud_status():
    if not is_admin(): abort(401)
    return jsonify(google_configured=gdrive.configured(),google_connected=gdrive.connected())
@app.route('/api/google-quota')
def google_quota():
    if not is_admin(): abort(401)
    if not gdrive.connected(): return jsonify(connected=False)
    try:
        data=gdrive.api('GET','https://www.googleapis.com/drive/v3/about',params={'fields':'storageQuota'}).get('storageQuota',{})
        limit=int(data['limit']) if data.get('limit') else None
        used=int(data.get('usage',0))
        return jsonify(connected=True,total=limit,used=used,free=max(0,limit-used) if limit is not None else None,folders=gdrive.root_links(),panel_folders=gdrive.subfolder_links())
    except Exception:
        return jsonify(connected=True,error='دریافت اطلاعات فضای Google Drive انجام نشد'),503
@app.route('/api/jobs',methods=['GET','POST'])
def jobs():
    if not is_admin(): abort(401)
    if request.method=='POST':
        links=request.json.get('links','').splitlines()
        if not links or len(links)>20: return jsonify(error='حداکثر ۲۰ لینک وارد کنید'),400
        mode=request.json.get('mode','debrid')
        if mode not in ('debrid','direct'): return jsonify(error='روش دانلود نامعتبر است'),400
        storage=request.json.get('storage','server'); visibility=request.json.get('visibility','private')
        if storage not in ('server','google') or visibility not in ('private','public'): return jsonify(error='مقصد یا نوع دسترسی نامعتبر است'),400
        if storage=='google' and not gdrive.connected(): return jsonify(error='ابتدا Google Drive را متصل کنید'),400
        for link in links:
            link=link.strip()
            if not (link.startswith('https://') or link.startswith('http://') or (mode=='debrid' and link.startswith('magnet:?'))): return jsonify(error='لینک نامعتبر است'),400
        folder_id=request.json.get('folder_id') or None
        if folder_id:
            with db() as c:
                if not c.execute('SELECT 1 FROM folders WHERE id=?',(folder_id,)).fetchone(): return jsonify(error='پوشه پیدا نشد'),400
        for link in links:
            id=newjob(link.strip(),folder_id); threading.Thread(target=work,args=(id,link.strip(),folder_id,mode,storage,visibility),daemon=True).start()
        return jsonify(ok=True)
    with db() as c: rows=c.execute('SELECT id,filename,status,progress,size,error,created,folder_id,downloads,storage,cloud_id,visibility FROM jobs ORDER BY created DESC LIMIT 200').fetchall()
    return jsonify([dict(x) for x in rows])
@app.route('/api/upload',methods=['POST'])
def upload():
    if not is_admin(): abort(401)
    size=request.content_length
    if size is None: return jsonify(error='حجم فایل مشخص نیست'),411
    storage=request.headers.get('X-Storage','server'); visibility=request.headers.get('X-Visibility','private')
    if storage not in ('server','google') or visibility not in ('private','public'): return jsonify(error='مقصد یا نوع دسترسی نامعتبر است'),400
    if storage=='google' and not gdrive.connected(): return jsonify(error='ابتدا Google Drive را متصل کنید'),400
    if storage=='server' and size>shutil.disk_usage(D).free-1024**3: return jsonify(error='فضای کافی روی سرور وجود ندارد'),413
    folder_id=request.headers.get('X-Folder-ID') or None
    if folder_id:
        with db() as c:
            if not c.execute('SELECT 1 FROM folders WHERE id=?',(folder_id,)).fetchone(): return jsonify(error='پوشه پیدا نشد'),400
    name=urllib.parse.unquote(request.headers.get('X-Filename','upload')).replace('\\','/')
    name=os.path.basename(name).replace('\x00','')[:220] or 'upload'
    id=newjob('uploaded file',folder_id); tmp=D/'files'/(id+'.part'); done=0; last=0
    try:
        if storage=='google':
            save_cloud(id,name,size,request.stream,visibility)
            return jsonify(ok=True,id=id)
        update(id,status='uploading',filename=name)
        with tmp.open('wb') as f:
            while True:
                chunk=request.stream.read(1024*1024)
                if not chunk: break
                if shutil.disk_usage(D).free < 1024**3: raise ValueError('Not enough free space')
                f.write(chunk); done+=len(chunk)
                if time.monotonic()-last>2:
                    update(id,progress=min(99,int(done*100/size)) if size else 0,size=done); last=time.monotonic()
        if done!=size: raise ValueError('Incomplete upload')
        finish_host(id,tmp); update(id,status='done',progress=100,size=done)
        return jsonify(ok=True,id=id)
    except Exception as e:
        tmp.unlink(missing_ok=True); update(id,status='failed',error=str(e)[:300]); return jsonify(error='آپلود کامل نشد'),400
@app.route('/api/storage')
def storage():
    if not is_admin(): abort(401)
    disk=shutil.disk_usage(D)
    return jsonify(total=disk.total,free=disk.free,used=disk.used)
@app.route('/api/folders',methods=['GET','POST'])
def folders():
    if not is_admin(): abort(401)
    if request.method=='POST':
        name=str((request.json or {}).get('name','')).strip()
        if not name or len(name)>80: return jsonify(error='نام پوشه باید بین ۱ تا ۸۰ کاراکتر باشد'),400
        storage=(request.json or {}).get('storage','server'); visibility=(request.json or {}).get('visibility','private')
        if storage not in ('server','google') or visibility not in ('private','public'): return jsonify(error='مقصد یا نوع دسترسی نامعتبر است'),400
        if storage=='google' and not gdrive.connected(): return jsonify(error='ابتدا Google Drive را متصل کنید'),400
        id=secrets.token_urlsafe(12)
        try:
            with db() as c: c.execute('INSERT INTO folders (id,name) VALUES (?,?)',(id,name))
        except sqlite3.IntegrityError: return jsonify(error='این پوشه از قبل وجود دارد'),409
        try:
            if storage=='google': gdrive.ensure_folder(visibility,id,name)
            else: host_folder(id,create=True)
        except Exception:
            with db() as c: c.execute('DELETE FROM folders WHERE id=?',(id,))
            return jsonify(error='ساخت پوشه در مقصد انجام نشد؛ اتصال و فضای مقصد را بررسی کنید'),502
        return jsonify(ok=True,id=id)
    with db() as c: rows=c.execute('SELECT id,name FROM folders ORDER BY name COLLATE NOCASE').fetchall()
    return jsonify([{**dict(r),'has_host_folder':host_folder(r['id']).is_dir()} for r in rows])
@app.route('/api/folders/<id>',methods=['POST'])
def folder_update(id):
    if not is_admin(): abort(401)
    data=request.json or {}
    with db() as c:
        folder=c.execute('SELECT name FROM folders WHERE id=?',(id,)).fetchone()
        if not folder: abort(404)
        if c.execute("SELECT 1 FROM jobs WHERE folder_id=? AND status IN ('queued','processing','downloading','uploading') LIMIT 1",(id,)).fetchone(): return jsonify(error='ابتدا انتقال‌های فعال این پوشه را کامل کنید'),409
        if data.get('delete') is True:
            jobs=c.execute("SELECT id,cloud_id,visibility FROM jobs WHERE folder_id=? AND storage='google' AND cloud_id IS NOT NULL",(id,)).fetchall()
            for job in jobs:
                try: gdrive.move_file(job['cloud_id'],gdrive.ensure_folder(job['visibility']))
                except Exception: return jsonify(error='انتقال فایل‌ها در Google Drive کامل نشد؛ دوباره تلاش کنید'),502
            local_jobs=c.execute("SELECT id FROM jobs WHERE folder_id=? AND storage='server' AND status='done'",(id,)).fetchall()
            for job in local_jobs:
                try:
                    source=existing_host_path(job['id'],id); target=host_path(job['id'])
                    if source.is_file() and source!=target: source.replace(target)
                except OSError: return jsonify(error='انتقال فایل‌های سرور کامل نشد؛ دوباره تلاش کنید'),502
            c.execute('UPDATE jobs SET folder_id=NULL WHERE folder_id=?',(id,))
            c.execute('DELETE FROM folders WHERE id=?',(id,))
            try: host_folder(id).rmdir()
            except OSError: pass
            if gdrive.connected():
                try: gdrive.forget_folder(id)
                except Exception: pass
        else:
            name=str(data.get('name','')).strip()
            if not name or len(name)>80: return jsonify(error='نام پوشه معتبر نیست'),400
            if c.execute('SELECT 1 FROM folders WHERE name=? AND id!=?',(name,id)).fetchone(): return jsonify(error='این پوشه از قبل وجود دارد'),409
            if gdrive.connected():
                try: gdrive.rename_folder(id,name,folder['name'])
                except Exception: return jsonify(error='تغییر نام پوشه در Google Drive انجام نشد'),502
            c.execute('UPDATE folders SET name=? WHERE id=?',(name,id))
    return jsonify(ok=True)
@app.route('/api/jobs/<id>/folder',methods=['POST'])
def move_job(id):
    if not is_admin(): abort(401)
    folder_id=(request.json or {}).get('folder_id') or None
    with db() as c:
        folder=c.execute('SELECT name FROM folders WHERE id=?',(folder_id,)).fetchone() if folder_id else None
        if folder_id and not folder: return jsonify(error='پوشه پیدا نشد'),400
        row=c.execute('SELECT status,storage,cloud_id,visibility,folder_id FROM jobs WHERE id=?',(id,)).fetchone()
        if not row: abort(404)
        if row['status'] not in ('done','failed'): return jsonify(error='پس از اتمام دانلود می‌توان فایل را جابه‌جا کرد'),409
        if row['storage']=='google' and row['cloud_id']:
            try: gdrive.move_file(row['cloud_id'],gdrive.ensure_folder(row['visibility'],folder_id,folder['name'] if folder else None))
            except Exception: return jsonify(error='جابجایی در Google Drive انجام نشد'),502
        elif row['storage']=='server' and row['status']=='done':
            try:
                source=existing_host_path(id,row['folder_id']); target=host_path(id,folder_id,create=True)
                if not source.is_file(): return jsonify(error='فایل روی سرور پیدا نشد'),404
                if source!=target: source.replace(target)
            except OSError: return jsonify(error='جابجایی فایل روی سرور انجام نشد'),502
        c.execute('UPDATE jobs SET folder_id=? WHERE id=?',(folder_id,id))
    return jsonify(ok=True)
@app.route('/api/delete/<id>',methods=['POST'])
def delete(id):
    if not is_admin(): abort(401)
    with db() as c:
        row=c.execute('SELECT status,storage,cloud_id,folder_id FROM jobs WHERE id=?',(id,)).fetchone()
        if not row: abort(404)
        if row['status'] not in ('done','failed'): return jsonify(error='دانلود هنوز فعال است'),409
        if row['storage']=='google' and row['cloud_id']:
            try: gdrive.remove(row['cloud_id'])
            except Exception: return jsonify(error='حذف فایل از Google Drive انجام نشد'),502
        else: existing_host_path(id,row['folder_id']).unlink(missing_ok=True)
        c.execute('DELETE FROM jobs WHERE id=?',(id,))
    return jsonify(ok=True)
@app.route('/download/<id>')
def file(id):
    with db() as c: row=c.execute('SELECT filename,status,folder_id,storage FROM jobs WHERE id=?',(id,)).fetchone()
    if not row or row['status']!='done': abort(404)
    if row['storage']!='server': abort(404)
    response=send_file(existing_host_path(id,row['folder_id']),as_attachment=True,download_name=row['filename'],conditional=True)
    range_header=request.headers.get('Range','')
    if request.method=='GET' and response.status_code in (200,206) and (not range_header or range_header.startswith('bytes=0-')):
        with db() as c: c.execute('UPDATE jobs SET downloads=downloads+1 WHERE id=?',(id,))
    return response
@app.route('/api/authorize-download')
def authorize_download():
    method=request.headers.get('X-Forwarded-Method','')
    path=urllib.parse.urlsplit(request.headers.get('X-Forwarded-Uri','')).path
    if method not in ('GET','HEAD') or not path.startswith('/download/'): abort(403)
    id=path[len('/download/'):]
    if not id or '/' in id: abort(404)
    with db() as c:
        row=c.execute('SELECT filename,status,folder_id,storage FROM jobs WHERE id=?',(id,)).fetchone()
        if not row or row['status']!='done' or row['storage']!='server': abort(404)
        try: target=existing_host_path(id,row['folder_id'])
        except ValueError: abort(404)
        if not target.is_file(): abort(404)
        range_header=request.headers.get('Range','')
        if method=='GET' and (not range_header or range_header.startswith('bytes=0-')):
            c.execute('UPDATE jobs SET downloads=downloads+1 WHERE id=?',(id,))
    response=Response('',200)
    response.headers['X-Download-Disposition']="attachment; filename*=UTF-8''"+urllib.parse.quote(row['filename'],safe='')
    response.headers['X-Download-Path']='/'+target.relative_to(D/'files').as_posix()
    return response
@app.route('/folder/<id>')
def public_host_folder(id):
    try: folder_exists=host_folder(id).is_dir()
    except ValueError: abort(404)
    if not folder_exists: abort(404)
    with db() as c:
        folder=c.execute('SELECT name FROM folders WHERE id=?',(id,)).fetchone()
        if not folder: abort(404)
        rows=c.execute("SELECT id,filename,size FROM jobs WHERE folder_id=? AND storage='server' AND status='done' ORDER BY created DESC",(id,)).fetchall()
    items=''.join('<li><a href="/download/'+html.escape(r['id'],quote=True)+'">'+html.escape(r['filename'] or 'download')+'</a><small>'+str(round(r['size']/1048576,1))+' MB</small></li>' for r in rows) or '<li>این پوشه هنوز فایلی ندارد.</li>'
    name=html.escape(folder['name'])
    page='''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+name+'''</title><style>body{background:#0b101c;color:#eef2fa;font:15px Tahoma;max-width:760px;margin:70px auto;padding:0 20px}h1{font-size:25px}p,small{color:#a8b6cb}ul{padding:0;list-style:none}li{border:1px solid #2b3950;border-radius:12px;background:#141e2e;padding:18px;margin:10px 0;display:flex;justify-content:space-between;gap:15px}a{color:#c3baff;overflow-wrap:anywhere}small{white-space:nowrap}</style><h1>'''+name+'''</h1><p>فایل‌های این پوشه روی سرور ذخیره شده‌اند.</p><ul>'''+items+'''</ul></html>'''
    return Response(page,mimetype='text/html')
@app.route('/logout',methods=['POST'])
def logout(): session.clear(); return redirect('/login')
HTML='''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>پنل لیچر</title><style>*{box-sizing:border-box}body{background:#0b1220;color:#e5e7eb;font:15px Tahoma;margin:0}main{max-width:1000px;margin:auto;padding:35px 20px}h1{font-size:26px}p{color:#a4b1c6;line-height:1.8}.card{background:#152136;border:1px solid #293750;border-radius:16px;padding:22px;margin:20px 0}textarea{width:100%;min-height:120px;background:#0b1423;border:1px solid #40516e;border-radius:8px;color:white;padding:14px;direction:ltr}button,.link{background:#6366f1;color:white;border:0;border-radius:8px;padding:10px 16px;cursor:pointer;text-decoration:none}.danger{background:#71323d}.row{border-top:1px solid #334155;padding:16px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}.name{flex:1;min-width:200px;overflow-wrap:anywhere}.muted{color:#a4b1c6;font-size:13px}.error{color:#fca5a5}</style><main><h1>پنل دانلود Real-Debrid</h1><p>لینک‌های پشتیبانی‌شده یا مگنت را وارد کنید؛ هر لینک در یک خط.</p><section class="card"><textarea id="links" placeholder="https://...&#10;magnet:?..." spellcheck="false"></textarea><p><button onclick="add()">شروع دانلود</button> <span id="msg"></span></p></section><section class="card"><h2>فایل‌ها</h2><div id="list">در حال بارگذاری...</div></section><form action="/logout" method="post"><button>خروج</button></form></main><script>
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function add(){let el=document.getElementById('links'); let r=await fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({links:el.value})});let j=await r.json();document.getElementById('msg').textContent=j.error||'به صف اضافه شد';if(r.ok)el.value='';load()}
async function del(id){if(!confirm('فایل حذف شود؟'))return;await fetch('/api/delete/'+id,{method:'POST'});load()}
async function load(){let r=await fetch('/api/jobs');if(!r.ok)return;let jobs=await r.json();document.getElementById('list').innerHTML=jobs.length?jobs.map(x=>`<div class="row"><div class="name"><b>${esc(x.filename||'در حال آماده‌سازی')}</b><p class="muted">${esc(({queued:'در صف',processing:'در حال آماده‌سازی',downloading:'در حال دانلود',done:'آماده',failed:'خطا'})[x.status])} · ${x.progress}% · ${(x.size/1048576).toFixed(1)} MB</p>${x.error?`<span class="error">${esc(x.error)}</span>`:''}</div>${x.status==='done'?`<a class="link" href="/download/${x.id}">دانلود</a> <button onclick="navigator.clipboard.writeText(location.origin+'/download/${x.id}')">کپی لینک</button>`:''}${['done','failed'].includes(x.status)?`<button class="danger" onclick="del('${x.id}')">حذف</button>`:''}</div>`).join(''):'هنوز فایلی وجود ندارد.'}load();setInterval(load,5000);
</script></html>'''
