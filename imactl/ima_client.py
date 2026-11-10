"""Drive the ima.copilot desktop client (under Wine) over CDP.

Boundary: only what the client itself does for its user - its knowledge-base page's own services
(the calls behind its buttons, made by the page with its own login), pressing the viewer's 下载
button, reading what the viewer renders. Nothing here calls ima's crypto bridge or fetches file
URLs itself; a document the client does not offer for download is read off the viewer.
"""
import base64, json, os, re, subprocess, threading, time, urllib.parse, urllib.request
from collections import Counter

import websocket

CDP = os.environ.get('IMA_CDP', 'http://127.0.0.1:9222')
DOWNLOADS = os.environ.get('IMA_DOWNLOADS', '/data/downloads')
KB_PAGE = 'chrome://allknowledge'
VIEWER_IDS = ('elhgpcianbbmeilccnddimfddkgegeee',)  # PDF viewer extension

# one client, one UI: serialize every operation
LOCK = threading.RLock()


class ImaError(Exception):
    pass


class Target:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({'id': self.n, 'method': method, 'params': params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get('id') == self.n:
                if 'error' in m:
                    raise ImaError(f"{method}: {m['error'].get('message')}")
                return m.get('result', {})

    def js(self, expr):
        r = self.call('Runtime.evaluate', expression=expr, returnByValue=True, awaitPromise=True)
        if 'exceptionDetails' in r:
            raise ImaError(r['exceptionDetails'].get('exception', {}).get('description', 'js error'))
        return r.get('result', {}).get('value')

    def events(self, seconds):
        self.ws.settimeout(1)
        end = time.time() + seconds
        while time.time() < end:
            try:
                yield json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue

    def close(self):
        self.ws.close()


def targets():
    try:
        return json.load(urllib.request.urlopen(CDP + '/json/list', timeout=10))
    except OSError as e:
        raise ImaError(f'ima client not reachable at {CDP} ({e}); is the container up?')


def page(prefix, wait=0.0):
    end = time.time() + wait
    while True:
        for t in targets():
            if t['type'] == 'page' and t['url'].startswith(prefix):
                try:
                    return Target(t['webSocketDebuggerUrl']), t
                except websocket.WebSocketException:
                    pass  # listed but already closing: look again
        if time.time() >= end:
            return None, None
        time.sleep(0.5)


def browser():
    v = json.load(urllib.request.urlopen(CDP + '/json/version', timeout=10))
    return Target(v['webSocketDebuggerUrl'])


# CSS-module class names carry build hashes: match on the stable prefix
JS_HELPERS = r"""
const $$ = (sel, root=document) => [...root.querySelectorAll(sel)];
const txt = e => (e.innerText || e.textContent || '').trim();
const items = () => $$('[class*="_knowledge_"]').filter(e => e.querySelector('[class*="_titleText_"]')).map(e => {
    const title = txt(e.querySelector('[class*="_titleText_"]'));
    const meta = txt(e).replace(title, '').trim().replace(/\s+/g, ' ');
    return {el: e, title, meta, folder: /\d+\s*项/.test(meta)};
});
const kbs = () => $$('[class*="_knowledgeListItem_"]').map(e => {
    const t = e.querySelector('[class*="_knowledgeListItemTitle_"]');
    return {el: e, name: t ? txt(t) : txt(e)};
});
const pick = (list, key, want) => list.find(x => x[key] === want) || list.find(x => x[key].startsWith(want))
                                  || list.find(x => x[key].includes(want));
"""


def _js(t, body):
    return t.js('(() => {' + JS_HELPERS + body + '})()')


def _js_until(t, body, ok, timeout=20):
    """Lists render their rows before their text (first load after login took >3s): poll."""
    end = time.time() + timeout
    while True:
        v = _js(t, body)
        if ok(v) or time.time() >= end:
            return v
        time.sleep(1)


# ---------- session ----------
def status():
    ts = targets()
    login = any(t['url'].startswith('chrome://login') for t in ts)
    version = None
    home, _ = page('chrome://home')
    if home:
        version = home.js("""new Promise(r => chrome.imaFrame.invokeWithCallback({action:'getDeviceInfo',params:'{}'},
            x => { try { r(JSON.parse(x.data).qua.match(/PPVN=([^&]+)/)[1]) } catch(e) { r(null) } }))""")
    return {'logged_in': not login, 'version': version}


def logged_in():
    return not any(t['url'].startswith('chrome://login') for t in targets())


def login_qr(fresh=True):
    """PNG of the WeChat login dialog. fresh: reload the QR first (they expire in minutes);
    also retried when it renders blank, which happens on the first load after start."""
    t, _ = page('chrome://login', wait=5)
    if not t:
        raise ImaError('not on the login page: already logged in')
    png = b''
    for attempt in range(3):
        if fresh or attempt:
            qr, _ = page('https://ima.qq.com/login', wait=2)
            if qr:
                qr.js('location.reload()')
            time.sleep(6)
        png = base64.b64decode(t.call('Page.captureScreenshot', format='png')['data'])
        if len(png) > 20000:  # a blank dialog compresses to a few KB
            break
    return png


def wait_login(seconds=180):
    end = time.time() + seconds
    while time.time() < end:
        if logged_in():
            return True
        time.sleep(2)
    return False


def screenshot():
    """The whole X display: native windows are not CDP targets. Only inside the container."""
    return subprocess.run(['import', '-window', 'root', 'png:-'], check=True, capture_output=True).stdout


def close():
    browser().ws.send(json.dumps({'id': 1, 'method': 'Browser.close'}))


# ---------- knowledge bases: the page's own services ----------
# The knowledge-base page is a React app whose HTTP services live in an inversify container,
# bound to Symbol.for(name). Calling them is what the page's own buttons do - same requests, same
# login headers, same token refresh - without clicking and waiting for rows to render.
SVC = r'''const svc = name => {
    if (!window.__imaDI) {
        const root = document.getElementById('root'), seen = new Set();
        const stack = [root && root[Object.keys(root).find(k => k.startsWith('__reactContainer'))]];
        while (stack.length && !window.__imaDI) {
            const f = stack.pop(); if (!f || seen.has(f)) continue; seen.add(f);
            const v = f.memoizedProps && f.memoizedProps.value;
            if (v && v._bindingDictionary && typeof v.get === 'function') window.__imaDI = v;
            if (f.child) stack.push(f.child); if (f.sibling) stack.push(f.sibling);
        }
        if (!window.__imaDI) throw new Error('the knowledge base page services were not found (another ima version?)');
    }
    return window.__imaDI.get(Symbol.for(name)) };
'''
KB_TYPES = {1001: 'personal', 1002: 'shared', 1003: 'subscribed', 1004: 'subscribed'}


def kb_page():
    t, _ = page(KB_PAGE)
    if not t:
        urllib.request.urlopen(urllib.request.Request(CDP + '/json/new?' + KB_PAGE + '/', method='PUT'), timeout=10)
        t, _ = page(KB_PAGE, wait=15)
        if not t:
            raise ImaError('could not open the knowledge base view (logged in?)')
        # the default (personal) KB loads late and a click during that load gets lost
        _wait(t, lambda v: bool(v['title']) and v['n'] > 0, 'first load', timeout=30)
    return t


def api(service, method, *args, what=None):
    """await svc(service)[method](*args) in the knowledge-base page; service may name a member,
    'OPEN_KNOWLEDGE_SERVICE.fileManagerHttpService'. The services return their error object (or
    null) instead of throwing: turn that into ImaError."""
    r = kb_page().js(f'''(async () => {{ {SVC}
        let o = null; for (const [i, p] of {json.dumps(service.split('.'))}.entries()) o = i ? o[p] : svc(p);
        const r = await o[{json.dumps(method)}](...{json.dumps(args)});
        return r === undefined ? {{}} : r instanceof Error ? {{error: r.message || String(r)}} : r }})()''')
    if r is None or (isinstance(r, dict) and ('error' in r or r.get('code') not in (None, 0))):
        msg = (r or {}).get('error') or (r or {}).get('msg') or 'no answer'
        raise ImaError(f'{what or method}: ima says "{msg}"')
    return r


def _pick(items, key, want):
    """Exact, then prefix, then substring match - tools accept names given as a prefix."""
    for ok in (lambda v: v == want, lambda v: v.startswith(want), lambda v: want in v):
        for x in items:
            if ok(x[key]):
                return x


def kbs():
    """[{id, name, type}] of all knowledge bases, asked from ima's server each time: a knowledge base
    subscribed on the phone shows up at once (the page's sidebar only loads on start).
    ponytail: first 50 per group (personal / shared / subscribed); page on nextCursor if anyone has more."""
    r = api('READER_SERVICE', 'getKnowledgeBaseList',
            {'params': [{'type': t, 'cursor': '', 'limit': 50} for t in KB_TYPES]}, what='knowledge base list')
    return [{'id': k['id'], 'name': k['basicInfo']['name'], 'type': KB_TYPES[g['type']]}
            for g in r['results'] for k in g.get('knowledgeBaseList') or []]


def kb(name):
    ks = kbs()
    k = _pick(ks, 'name', name) or next((k for k in ks if k['id'] == name), None)
    if not k:
        raise ImaError(f'no knowledge base matching {name!r}; have: {[k["name"] for k in ks]}')
    return k


def _list(kb_id, folder_id, limit=500):
    """Raw entries of a folder (the KB root's folder id is the KB id), following the cursor."""
    out, cursor = [], ''
    while True:
        r = api('READER_SERVICE', 'getKnowledgeList', {'knowledgeBaseId': kb_id, 'folderId': folder_id, 'cursor': cursor,
                                                       'limit': 50, 'sortType': 9}, what='listing')
        out += r.get('knowledgeList') or []
        cursor = r.get('nextCursor')
        if r.get('isEnd') or not cursor or len(out) >= limit:
            return out, not r.get('isEnd')


def _folder(k, folders, names=None):
    """Folder id of the path `folders` (names or prefixes) inside knowledge base k; appends the
    full folder names to `names`."""
    fid = k['id']
    for name in folders:
        subs = [i for i in _list(k['id'], fid)[0] if i.get('folderInfo')]
        f = _pick(subs, 'title', name)
        if not f:
            raise ImaError(f'no folder {name!r} here; folders: {[i["title"] for i in subs]}')
        fid = f['folderInfo']['folderId']
        if names is not None:
            names.append(f['title'])
    return fid


def ls(kb_name, folders=()):
    k, path = kb(kb_name), []
    items, more = _list(k['id'], _folder(k, folders, path))
    out = [{'type': 'folder', 'title': i['title'], 'items': int(i['folderInfo'].get('fileNumber') or 0)}
           if i.get('folderInfo') else
           {'type': 'document', 'title': i['title'], 'kind': (i.get('mediaTypeInfo') or {}).get('name'),
            'updated': i.get('timeWording')} for i in items]
    return {'path': [k['name'], *path], 'items': out, 'more': more}


def _search(kb_id, query):
    r = api('READER_SERVICE', 'searchKnowledge', {'query': query, 'knowledgeBaseId': kb_id, 'comparedKnowledgeBaseId': kb_id,
                                                  'policy': 0, 'cursor': ''}, what='search')
    return [{**x['knowledge'], 'snippet': x.get('highlightContent') or x['knowledge'].get('introduction') or ''}
            for x in r.get('searchedKnowledgeList') or [] if not x['knowledge'].get('folderInfo')]


def search(kb_name, query, limit=20):
    """The knowledge base's own full-text search. Returns [{title, snippet}], best first."""
    return [{'title': h['title'], 'snippet': re.sub(r'\s+', ' ', re.sub(r'</?em>', '', h['snippet'])).strip()[:300]}
            for h in _search(kb(kb_name)['id'], query)[:limit]]


def _media(k, title, folders):
    """The entry for document `title`: from its folder when given, else found with the KB's search.
    The search is semantic: a long query (the whole title) can rank the document out, so shorten it."""
    if folders:
        m = _pick([i for i in _list(k['id'], _folder(k, folders))[0] if not i.get('folderInfo')], 'title', title)
    else:
        m = next((m for q in dict.fromkeys((title, title[:12], title[:6]))
                  for m in [_pick(_search(k['id'], q), 'title', title)] if m), None)
    if not m:
        raise ImaError(f'{title!r} not found in {k["name"]!r}; try ima_search with a shorter query')
    return m


class Restricted(ImaError):
    """The knowledge base's owner does not let subscribers open this file (ima: 文件被设为不可查看)."""


def open_doc(k, m):
    """Open a document in ima's viewer, as clicking it does. Returns the viewer target. getMedia
    first is the page's own check: it says whether the client will open the file at all."""
    ref = {'knowledgeBaseId': k['id'], 'mediaId': m['mediaId']}
    g = api('OPEN_KNOWLEDGE_SERVICE.fileManagerHttpService', 'getMedia', ref, what='opening')
    if g.get('action') != 1:  # 1 Open; 2 Toast, 3 Abstract: ima shows a toast instead of the file
        raise Restricted(f'{m["title"]}: ima says "{g.get("toastText") or "暂时无法查看具体内容"}" - the knowledge base '
                         f'owner does not let subscribers open this file. ima_ask can still query the knowledge base about it.')
    before = {x['id'] for x in targets()}
    api('OPEN_KNOWLEDGE_SERVICE', 'openKnowledgeMedia', ref, what='opening')
    end = time.time() + 30
    while time.time() < end:
        new = [x for x in targets() if x['id'] not in before and x['type'] == 'page']
        if new:
            return new[0]
        time.sleep(0.3)
    raise ImaError(f'{m["title"]}: no viewer appeared within 30s')


def refresh():
    """Reload the knowledge-base page. Its sidebar is loaded once; ask() clicks in it."""
    t = kb_page()
    t.js('window.__imaStale = 1; location.reload(); 1')
    end = time.time() + 30
    while time.time() < end:
        time.sleep(0.5)
        try:
            if t.js('!window.__imaStale && document.readyState === "complete"'):
                break
        except ImaError:
            pass  # mid-navigation
    _wait(t, lambda v: bool(v['title']) and v['n'] > 0, 'reload', timeout=30)


def subscribe(link):
    """Subscribe to (join) a knowledge base from its share link, https://ima.qq.com/wiki/?shareId=...,
    or the bare shareId: what its 订阅 / 加入 button does. Paid knowledge bases are refused."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(link.strip()).query)
    share = (q.get('shareId') or [link.strip()])[0]
    r = api('KNOWLEDGE_BASE_SHARE_SERVICE', 'getKnowledgeBaseInfoFromShareId', {'shareId': share},
            what=f'looking up share id {share!r} (is the link complete?)')
    info = r.get('knowledgeBaseInfo')
    if not info:
        raise ImaError(f'no knowledge base behind share id {share!r}')
    name, user = info['basicInfo']['name'], info.get('userPermissionInfo') or {}
    if user.get('roleType'):  # 0 = visitor
        return {'name': name, 'status': 'already subscribed'}
    if user.get('isInApplyList'):
        return {'name': name, 'status': 'already applied, waiting for the owner to approve'}
    if int((info.get('paymentInfo') or {}).get('defaultPrice') or 0):
        raise ImaError(f'{name!r} is a paid knowledge base: subscribe to it in the ima app yourself')
    j = api('SHARE_KNOWLEDGE_MEMBER', 'joinKnowledge', {
        'knowledgeBaseId': info['id'], 'memberCount': int((info.get('memberInfo') or {}).get('memberCount') or 0) + 1,
        'applyReason': '', 'inviteToken': (q.get('inviteToken') or [''])[0]}, what='subscribing')
    refresh()
    return {'name': name, 'status': 'applied, waiting for the owner to approve' if j.get('needReview') else 'subscribed'}


# The UI is still driven for one thing, the knowledge base's AI Q&A box (ask): that answer streams
# into the page. The page title is the open KB's full name.
VIEW = 'return {title: document.title, n: items().length, first: items().slice(0, 5).map(i => i.title)}'


def _view(t):
    return _js(t, VIEW)


def _wait(t, ok, what, timeout=10):
    """Poll until ok(view) holds on two reads in a row (rows render in steps; one read can catch
    the old folder's rows under the new title)."""
    end, last = time.time() + timeout, None
    while True:
        v = _view(t)
        if ok(v) and v == last:
            return v
        if time.time() >= end:
            if ok(v):
                return v
            raise ImaError(f'{what}: the knowledge base view did not get there within {timeout}s')
        last = v
        time.sleep(0.2)


def open_kb(name):
    """Show knowledge base `name` in the page. Clicks nothing when it is already open."""
    full = kb(name)['name']
    t = kb_page()
    sidebar = lambda: _js_until(t, 'return kbs().map(k => k.name)', lambda v: v and all(v))
    if full not in sidebar():
        refresh()  # subscribed after the page loaded
        if full not in sidebar():
            raise ImaError(f'{full!r} is not in the client\'s sidebar')
    if _view(t)['title'] != full:
        for attempt in range(3):  # a click while the page loads can get lost
            _js(t, f'pick(kbs(), "name", {json.dumps(full)}).el.click(); return 1')
            try:
                _wait(t, lambda v: v['title'] == full and v['n'] > 0, f'knowledge base {full!r}', timeout=8)
                break
            except ImaError:
                if attempt == 2:
                    raise
    return full


def ask(kb, question, timeout=300):
    """Ask the knowledge base's own AI Q&A (the 基于知识库提问 box). This still works when the owner
    made files not viewable (文件被设为不可查看). Returns {answer, sources}; the answer is ima's
    model summarising the library, not the original text."""
    with LOCK:
        open_kb(kb)  # the Q&A box is there in every view
        t = kb_page()
        bubbles = 'return $$(\'[class*="_normalModeAiBubbleWrapper_"]\').filter(x => x.offsetWidth > 0)'
        n0 = len(_js(t, bubbles + '.map(x => 1)'))
        if not _js(t, '''const e = $$(".tiptap[contenteditable=true]").find(x => x.offsetWidth > 0); if (!e) return false;
                         e.focus(); document.execCommand("selectAll"); document.execCommand("delete"); return true'''):
            raise ImaError('the knowledge base Q&A box was not found')
        t.call('Input.insertText', text=question)
        time.sleep(0.5)
        if not _js(t, 'const b = $$(".icon-send").find(x => x.offsetParent); if (!b) return false; b.click(); return true'):
            raise ImaError('the Q&A send button was not found')
        last, stable, end = None, 0, time.time() + timeout
        while time.time() < end:
            time.sleep(2)
            cur = _js(t, bubbles + '''.slice(%d).map(b => ({text: (b.querySelector('[class*="_bubble_"]') || b).innerText || '',
                sources: [...b.querySelectorAll('[class*="_sourceInfo_"]')].map(s => (s.innerText || '').trim())}))''' % n0)
            if cur and cur[-1]['text'].strip():
                stable = stable + 1 if cur[-1] == last else 0
                last = cur[-1]
                if stable >= 4:  # 8 s without a change: generation is over
                    break
        if not last:
            raise ImaError(f'no answer within {timeout}s')
        return {'answer': last['text'].strip(), 'sources': list(dict.fromkeys(s for s in last['sources'] if s))}


# ---------- documents ----------

def _wait_viewer_ready(vt):
    # the toolbar exists before the document has loaded; clicks then do nothing
    for _ in range(60):
        if vt.js("document.readyState === 'complete' && !!document.querySelector('[data-page-number]')"):
            return
        time.sleep(1)
    raise ImaError('viewer did not finish loading in 60s')


CLICK_DOWNLOAD = """(() => { const b = [...document.querySelectorAll('button,div,span')]
    .find(e => e.childElementCount <= 2 && (e.textContent || '').trim() === '下载');
    if (!b) return null; b.click(); return true })()"""


def download(viewer, out=DOWNLOADS, local=None):
    """Press 下载 in the viewer. Returns the local path, or None if the client offers no download.
    out: directory as Wine/this container sees it; local: same directory as the caller sees it."""
    local = local or os.environ.get('IMA_DOWNLOADS_LOCAL', out)
    os.makedirs(local, exist_ok=True)
    vt = Target(viewer['webSocketDebuggerUrl'])
    _wait_viewer_ready(vt)
    b = browser()
    b.call('Browser.setDownloadBehavior', behavior='allow', eventsEnabled=True,
           downloadPath='Z:' + out.replace('/', '\\'))  # Wine maps / to Z:
    name = None
    for _ in range(3):
        if not vt.js(CLICK_DOWNLOAD):
            return None
        for m in b.events(15):
            if m.get('method') == 'Browser.downloadWillBegin':
                name = m['params'].get('suggestedFilename')
                break
        if name:
            break
    if not name:
        raise ImaError('pressed 下载 three times, no download started')
    for m in b.events(300):
        p = m.get('params', {})
        if m.get('method') == 'Browser.downloadProgress' and p.get('state') in ('completed', 'canceled'):
            if p['state'] == 'canceled':
                raise ImaError(f'download of {name!r} was canceled by the client')
            path = os.path.join(local, name)
            try:  # Windows "from the internet" marker
                os.remove(path + ':Zone.Identifier')
            except OSError:
                pass
            return path
    raise ImaError('download did not finish within 300s')


def read_viewer(viewer):
    """Fallback for documents without 下载: scroll every page into view so the lazy viewer renders
    it, then collect its text layer. Returns [page_text, ...]."""
    vt = Target(viewer['webSocketDebuggerUrl'])
    _wait_viewer_ready(vt)
    n = vt.js("document.querySelectorAll('[data-page-number]').length")
    pages = []
    for i in range(1, n + 1):
        text = ''
        for _ in range(20):
            text = vt.js(f"""(() => {{ const p = document.querySelector('[data-page-number="{i}"]');
                p.scrollIntoView(); const t = p.querySelector('.textLayer');
                return t && t.childElementCount ? t.innerText : null }})()""")
            if text is not None:
                break
            time.sleep(0.5)
        pages.append(text or '')
    return pages


def close_target(t):
    browser().call('Target.closeTarget', targetId=t['id'])


# ---------- text ----------
def is_common(ch):
    """A character an ordinary Chinese text uses: in GB2312 or Big5. Garbage decodes land in
    CJK Extension A, compatibility ideographs, radicals - which neither set has."""
    for enc in ('gb2312', 'big5'):
        try:
            if len(ch.encode(enc)) == 2:
                return True
        except UnicodeEncodeError:
            pass
    return False


# ---------- text layer repair: Source Han CID subsets with a broken/partial ToUnicode ----------
CIDMAPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cidmaps')
_cidmaps = {}


def cidmaps():
    """CID -> code point per (family, region, release), built by tools/build_cidmaps.py."""
    if not _cidmaps and os.path.isdir(CIDMAPS):
        import gzip
        for n in sorted(os.listdir(CIDMAPS)):
            if n.endswith('.json.gz'):
                with gzip.open(os.path.join(CIDMAPS, n), 'rt', encoding='utf-8') as f:
                    _cidmaps[n[:-8]] = {int(k): v for k, v in json.load(f).items()}
    return _cidmaps


def _pdf_fonts(res, seen):
    """Fonts of a resource dict and of the form XObjects it uses, each once (pypdf objects)."""
    res = res.get_object() if res is not None else {}
    for ref in (res.get('/Font') or {}).values():
        f = ref.get_object()
        key = id(f) if ref is f else (ref.idnum, ref.generation)
        if key not in seen:
            seen.add(key)
            yield f
    for ref in (res.get('/XObject') or {}).values():
        x = ref.get_object()
        if x.get('/Subtype') == '/Form' and '/Resources' in x:
            yield from _pdf_fonts(x['/Resources'], seen)


def _tounicode_stream(mapping):
    out = [b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Adobe-Identity-UCS def '
           b'/CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange']
    items = sorted(mapping.items())
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        out.append(b'%d beginbfchar' % len(chunk))
        out += [b'<%04X> <%s>' % (c, s.encode('utf-16-be').hex().upper().encode()) for c, s in chunk]
        out.append(b'endbfchar')
    out.append(b'endcmap CMapName currentdict /CMap defineresource pop end end')
    return b'\n'.join(out)


def repair_text_layer(path):
    """Rewrite the ToUnicode of Source Han / Noto CJK CID subsets from the full fonts' tables.
    The subsets keep the original CIDs (Identity-H: code == CID); which release/region numbering
    applies is decided by decoding with every table and keeping the one whose output is ordinary
    text (>= 85% GB2312/Big5 characters; the wrong release scores ~27%). Visuals are untouched.
    Returns [(font, table, score)] for the fonts rewritten; saves the file in place if any.
    pypdf + fontTools, both pure Python: pikepdf would pull lxml and Pillow (~75 MB)."""
    import io
    from fontTools.cffLib import CFFFontSet
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    tables = cidmaps()
    fixes = []
    pdf = PdfWriter(clone_from=path)
    seen = set()
    for page in pdf.pages:
        for f in _pdf_fonts(page.get('/Resources'), seen):
                base = str(f.get('/BaseFont', ''))
                family = 'Serif' if 'Serif' in base else 'Sans'
                if f.get('/Subtype') != '/Type0' or str(f.get('/Encoding')) not in ('/Identity-H', '/Identity-V') \
                        or not ('SourceHan' in base or ('Noto' in base and 'CJK' in base)):
                    continue
                try:
                    desc = f['/DescendantFonts'][0].get_object()['/FontDescriptor'].get_object()
                    cff = CFFFontSet()
                    cff.decompile(io.BytesIO(desc['/FontFile3'].get_object().get_data()), None)
                    top = cff[cff.fontNames[0]]
                except Exception:
                    continue
                if not hasattr(top, 'ROS'):
                    continue
                cids = [int(g[3:]) for g in top.charset if g.startswith('cid')]
                # rank by how many glyphs decode to ordinary characters (rewards the release whose
                # numbering covers the subset, e.g. Serif 1.000 over 1.001 for 持), gate on the ratio
                best = None
                for name, t in tables.items():
                    if family not in name:
                        continue
                    dec = [chr(t[c]) for c in cids if c in t and t[c] >= 0x2E80]
                    if len(dec) < 5:
                        continue
                    common = sum(map(is_common, dec))
                    rank = (common, -sum(c not in t for c in cids))
                    if common / len(dec) >= 0.85 and (not best or rank > best[0]):
                        best = (rank, name, t, common / len(dec))
                if not best:
                    continue
                _, name, t, score = best
                tu = {}
                if '/ToUnicode' in f:
                    for a, b in re.findall(rb'<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]{4,8})>',
                                           f['/ToUnicode'].get_object().get_data()):
                        try:
                            tu[int(a, 16)] = bytes.fromhex(b.decode()).decode('utf-16-be')
                        except ValueError:
                            pass
                both = [c for c in cids if c in t and c in tu]
                if both and sum(tu[c] == chr(t[c]) for c in both) / len(both) >= 0.95 \
                        and all(c in tu for c in cids):
                    continue  # the producer's ToUnicode is fine already
                # glyphs no release table knows: U+FFFD, an honest "unknown", rather than keeping a
                # producer entry from a ToUnicode we have just shown to be wrong
                full = {c: chr(t[c]) if c in t else '�' for c in cids}
                s = DecodedStreamObject()
                s.set_data(_tounicode_stream(full))
                f[NameObject('/ToUnicode')] = pdf._add_object(s)
                fixes.append((base, name, round(score, 3), sum(c not in t for c in cids)))
    if fixes:
        tmp = path + '.repair'
        with open(tmp, 'wb') as out:
            pdf.write(out)
        os.replace(tmp, path)
    return fixes


def struct_alt_text(path):
    """{page number: [text]} from /ActualText and /Alt of a tagged PDF's structure tree. Producers
    that rasterise text (Aspose: each run an image under an invisible .notdef text layer) often
    still write the real words there for accessibility - the only exact source for such pages."""
    from pypdf import PdfReader
    r = PdfReader(path)
    root = r.trailer['/Root'].get_object().get('/StructTreeRoot')
    if root is None:
        return {}
    pages = {p.indirect_reference.idnum: n for n, p in enumerate(r.pages, 1)}
    out = {}

    def walk(node, page, depth):
        node = node.get_object() if hasattr(node, 'get_object') else node
        if depth > 200:
            return
        if isinstance(node, list):
            for k in node:
                walk(k, page, depth + 1)
            return
        if not hasattr(node, 'get'):
            return
        pg = node.get('/Pg')
        page = pages.get(pg.idnum, page) if pg is not None and hasattr(pg, 'idnum') else page
        text = str(node.get('/ActualText') or node.get('/Alt') or '').replace('\r\n', '\n').replace('\r', '\n').strip()
        if text and page:
            out.setdefault(page, []).append(text)
            return  # the element's text stands for its children
        if '/K' in node:
            walk(node['/K'], page, depth + 1)

    walk(root.get('/K'), None, 0)
    return out


def looks_garbled(text):
    """A page whose text layer is one character repeated (机机机机 / 与与与与). Seen in Aspose-made
    PDFs: every text run is rasterised (a 2x2 colour image with a text-shaped soft mask) under an
    invisible text layer of .notdef glyphs that a broken ToUnicode maps to one character. The words
    are not in the file except, sometimes, as /Alt text in the structure tree (struct_alt_text);
    the rest has to be read as images. A page with no text at all is a scan (one JPEG per page)
    or a blank page: also only readable as an image."""
    if not text.strip():
        return True
    chars = [c for c in text if not c.isspace() and not c.isascii()]
    if len(chars) < 15:  # covers short cover pages too
        return False
    top = Counter(chars).most_common(1)[0][1]
    if top >= 8 and top / len(chars) > 0.3:
        return True
    # unrepaired CID fonts decode to rare characters (Ext-A, compatibility forms, radicals)
    cjk = [c for c in chars if ord(c) >= 0x2E80]
    return len(cjk) >= 30 and sum(map(is_common, cjk)) / len(cjk) < 0.5


# poppler-utils instead of PyMuPDF: same text, and PyMuPDF 2.x drags in ~270 MB (onnxruntime, numpy)
def pdf_pages(path):
    text = subprocess.run(['pdftotext', '-enc', 'UTF-8', path, '-'], check=True, capture_output=True).stdout
    pages = text.decode('utf-8', 'replace').split('\f')  # one form feed after every page
    return pages[:-1] if pages and not pages[-1].strip() else pages


def render_pages(path, pages, width=1400):
    """JPEG per page, longest side `width` px: a photo-heavy page as PNG runs to ~8 MB."""
    return [subprocess.run(['pdftoppm', '-jpeg', '-jpegopt', 'quality=80', '-scale-to', str(width),
                            '-f', str(n), '-l', str(n), path], check=True, capture_output=True).stdout
            for n in pages]


def parse_pages(spec, total):
    """'1-3,7' -> [1,2,3,7] (1-based, clipped to the document)."""
    if not spec:
        return list(range(1, total + 1))
    got = []
    for part in spec.split(','):
        a, _, b = part.strip().partition('-')
        got += range(int(a), int(b or a) + 1)
    return [p for p in got if 1 <= p <= total]


def get_document(kb_name, title, folders=(), keep_viewer=False):
    """Full text of a document. Downloads it through the viewer when offered, else reads the viewer."""
    with LOCK:
        k = kb(kb_name)
        m = _media(k, title, folders)
        viewer = open_doc(k, m)
        try:
            file = download(viewer)
            repaired, alt = [], {}
            if file:
                try:
                    repaired = repair_text_layer(file)
                except Exception as e:  # a repair failure must not cost us the document
                    repaired = [('repair failed', str(e)[:200], 0)]
                texts, source = pdf_pages(file), 'download'
                try:
                    alt = struct_alt_text(file)
                except Exception:
                    alt = {}
            else:
                texts, source = read_viewer(viewer), 'viewer'
        finally:
            if not keep_viewer:
                close_target(viewer)  # a viewer costs ~250 MB
        return {
            'title': m['title'], 'source': source, 'file': file, 'pages': texts, 'repaired': repaired,
            'alt': {str(k): v for k, v in alt.items()},
            'garbled_pages': [i + 1 for i, t in enumerate(texts) if looks_garbled(t)],
        }


if __name__ == '__main__':  # self-check of the pure helpers
    assert parse_pages('1-3,7,99', 10) == [1, 2, 3, 7]
    assert parse_pages('', 3) == [1, 2, 3]
    assert looks_garbled('机' * 50 + '的')
    assert looks_garbled(' \n\n')  # a scanned page
    assert looks_garbled('2026年中国磁悬浮柔性输送系统研究报告' + '与' * 13)  # a garbled cover page
    assert not looks_garbled('目录 磁悬浮柔性输送系统产品概要 中国市场分析 本土重点企业分析')
    assert not looks_garbled('人形机器人关节模组市场规模预计在二零三零年达到数百亿元，主要驱动力来自产业链降本与应用落地。' * 2)
    print('ok')
