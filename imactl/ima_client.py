"""Drive the ima.copilot desktop client (under Wine) over CDP, the way a user would.

Boundary: only UI actions a user can take - clicking items, pressing the viewer's own 下载
button, reading what the viewer renders. Nothing here calls ima's crypto bridge or fetches
protected URLs; a document the client does not offer for download is read off the viewer.
"""
import base64, json, os, re, subprocess, threading, time, urllib.request
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
                return Target(t['webSocketDebuggerUrl']), t
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
const crumbs = () => $$('nav.ant-breadcrumb li').map(txt);
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


# ---------- knowledge bases ----------
def kb_page():
    t, _ = page(KB_PAGE)
    if not t:
        urllib.request.urlopen(urllib.request.Request(CDP + '/json/new?' + KB_PAGE + '/', method='PUT'), timeout=10)
        t, _ = page(KB_PAGE, wait=15)
        if not t:
            raise ImaError('could not open the knowledge base view (logged in?)')
        time.sleep(3)  # the sidebar renders after the first API round trip
    return t


def list_kbs():
    return _js_until(kb_page(), 'return kbs().map(k => k.name)', lambda v: v and all(v))


SNAPSHOT = 'return JSON.stringify([crumbs(), items().slice(0, 5).map(i => i.title)])'


def _await_change(t, before, timeout=4):
    """After a click the old rows stay on screen for a moment: reading them then would list the
    previous folder. Wait until the view differs from what it was before the click.
    ponytail: no change within 4s = we clicked where we already were (new rows took <2s in
    tests); the sidebar has no selected-state marker to check instead."""
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        now = _js(t, SNAPSHOT)
        if now != before and json.loads(now)[1]:
            return
    # same content is possible (clicked the folder already shown); fall through


def open_kb(name):
    t = kb_page()
    list_kbs()
    before = _js(t, SNAPSHOT)
    r = _js(t, f'const k = pick(kbs(), "name", {json.dumps(name)}); if (!k) return null; k.el.click(); return k.name')
    if not r:
        raise ImaError(f'no knowledge base matching {name!r}; have: {list_kbs()}')
    _await_change(t, before)
    return r


def ls():
    # an empty folder just waits out the timeout
    r = _js_until(kb_page(), 'return {path: crumbs(), items: items().map(i => ({type: i.folder ? "folder" : "document", title: i.title, meta: i.meta}))}',
                  lambda v: v['items'] and all(i['title'] for i in v['items']), timeout=10)
    return {'path': [p for p in r['path'] if p], 'items': r['items']}


def cd(name):
    t = kb_page()
    ls()  # wait for the rows to render
    before = _js(t, SNAPSHOT)
    if name == '..':
        r = _js(t, 'const c = $$("nav.ant-breadcrumb li"); if (c.length < 2) return null; '
                   'const l = c[c.length-2].querySelector("a,span"); l.click(); return txt(l)')
    else:
        r = _js(t, f'const i = pick(items().filter(x => x.folder), "title", {json.dumps(name)}); '
                   'if (!i) return null; i.el.click(); return i.title')
    if not r:
        raise ImaError(f'no folder {name!r} here; items: {[i["title"] for i in ls()["items"]]}')
    _await_change(t, before)
    return r


def goto(kb, path=()):
    open_kb(kb)
    for folder in path:
        cd(folder)


# ---------- documents ----------
def _viewer(wait):
    end = time.time() + wait
    while True:
        for t in targets():
            if t['type'] == 'page' and any(v in t['url'] for v in VIEWER_IDS):
                return t
        if time.time() >= end:
            return None
        time.sleep(0.5)


def open_doc(title):
    ls()  # wait for the rows to render
    before = {x['id'] for x in targets()}
    r = _js(kb_page(), f'const i = pick(items().filter(x => !x.folder), "title", {json.dumps(title)}); '
                       'if (!i) return null; i.el.click(); return i.title')
    if not r:
        raise ImaError(f'no document {title!r} here; items: {[i["title"] for i in ls()["items"]]}')
    end = time.time() + 30
    while time.time() < end:
        new = [x for x in targets() if x['id'] not in before and x['type'] == 'page']
        if new:
            return r, new[0]
        time.sleep(0.5)
    raise ImaError(f'{r}: no viewer appeared within 30s')


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
def looks_garbled(text):
    """Broken ToUnicode maps turn every glyph into the same character (机机机机 / 与与与与)."""
    chars = [c for c in text if not c.isspace() and not c.isascii()]
    if len(chars) < 40:
        return False
    return Counter(chars).most_common(1)[0][1] / len(chars) > 0.3


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


def get_document(kb, title, path=(), keep_viewer=False):
    """Full text of a document. Downloads it through the viewer when offered, else reads the viewer."""
    with LOCK:
        goto(kb, path)
        name, viewer = open_doc(title)
        try:
            file = download(viewer)
            if file:
                texts, source = pdf_pages(file), 'download'
            else:
                texts, source = read_viewer(viewer), 'viewer'
        finally:
            if not keep_viewer:
                close_target(viewer)  # a viewer costs ~250 MB
        return {
            'title': name, 'source': source, 'file': file, 'pages': texts,
            'garbled_pages': [i + 1 for i, t in enumerate(texts) if looks_garbled(t)],
        }


if __name__ == '__main__':  # self-check of the pure helpers
    assert parse_pages('1-3,7,99', 10) == [1, 2, 3, 7]
    assert parse_pages('', 3) == [1, 2, 3]
    assert looks_garbled('机' * 50 + '的')
    assert not looks_garbled('人形机器人关节模组市场规模预计在二零三零年达到数百亿元，主要驱动力来自产业链降本与应用落地。' * 2)
    print('ok')
