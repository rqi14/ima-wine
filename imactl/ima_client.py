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
        # let the default (personal) KB finish loading: a click during that load gets lost
        _await_view(t, lambda v: bool(v['title']), 'knowledge base view', timeout=30)
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


def _await_view(t, ok, what, timeout=20):
    """Wait until the view is the one we asked for, judged by identity rather than by 'something
    changed': on the first load the default (personal) knowledge base renders late, after our
    click, and change-detection then happily returned the personal KB's rows."""
    end = time.time() + timeout
    while True:
        v = _js(t, 'return {title: document.title, crumbs: crumbs(), n: items().length}')
        if ok(v) and v['n']:
            return
        if time.time() >= end:
            if ok(v):
                return  # the right view, just empty
            raise ImaError(f'{what} did not open within {timeout}s')
        time.sleep(0.5)


def open_kb(name):
    t = kb_page()
    list_kbs()
    before = _js(t, SNAPSHOT)
    click = f'const k = pick(kbs(), "name", {json.dumps(name)}); if (!k) return null; k.el.click(); return k.name'
    for attempt in range(3):
        r = _js(t, click)
        if not r:
            raise ImaError(f'no knowledge base matching {name!r}; have: {list_kbs()}')
        try:  # the page title is the open knowledge base's full name
            _await_view(t, lambda v: v['title'] == r, f'knowledge base {r!r}', timeout=8)
            break
        except ImaError:
            if attempt == 2:
                raise
    _await_change(t, before)  # clicking the open KB resets it to its root: let that land
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
    if name == '..':
        _await_change(t, before)
    else:  # the breadcrumb's last entry is the open folder
        _await_view(t, lambda v: v['crumbs'] and v['crumbs'][-1] == r, f'folder {r!r}')
    return r


def goto(kb, path=()):
    open_kb(kb)
    for folder in path:
        cd(folder)


def search(kb, query):
    """The knowledge base's own full-text search (the magnifier next to 内容). Leaves the view on
    the results, so open_doc() can click one. Returns [{title, snippet}], one per document."""
    open_kb(kb)
    t = kb_page()
    before = _js(t, SNAPSHOT)
    ok = _js(t, '''const ed = () => $$('[contenteditable][data-placeholder="在知识库中搜索"]').find(x => x.offsetWidth > 0);
        if (!ed()) { const b = $$('.icon-search-normal[class*="_titleIcon_"]').find(e => e.offsetParent); if (b) b.click(); }
        return true''')
    time.sleep(1)
    ok = _js(t, '''const e = $$('[contenteditable][data-placeholder="在知识库中搜索"]').find(x => x.offsetWidth > 0);
        if (!e) return false; e.focus(); document.execCommand('selectAll'); document.execCommand('delete'); return true''')
    if not ok:
        raise ImaError('knowledge base search box not found')
    t.call('Input.insertText', text=query)
    for typ in ('keyDown', 'keyUp'):
        t.call('Input.dispatchKeyEvent', type=typ, key='Enter', code='Enter', windowsVirtualKeyCode=13)
    _await_change(t, before, timeout=15)
    seen, out = set(), []
    for i in ls()['items']:
        if i['type'] == 'document' and i['title'] not in seen:
            seen.add(i['title'])
            snippet = re.sub(r'\s*\S+\s+\S+$', '', i['meta'])  # drop the trailing "PDF 5/20"
            out.append({'title': i['title'], 'snippet': snippet.replace('\\n', ' ').strip()})
    return out


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
    the rest has to be read as images."""
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


def find(kb, title):
    """Leave the KB's search results showing `title`. The search is semantic: a long query (the
    whole title) can rank the document out of the results, so shorten the query until it shows."""
    for q in dict.fromkeys((title, title[:12], title[:6])):
        if any(h['title'].startswith(title) or title.startswith(h['title']) for h in search(kb, q)):
            return
    raise ImaError(f'{title!r} not found by searching {kb!r}; try ima_search with a shorter query')


def get_document(kb, title, path=(), keep_viewer=False):
    """Full text of a document. Downloads it through the viewer when offered, else reads the viewer."""
    with LOCK:
        if path:
            goto(kb, path)
        else:  # titles from OpenAPI search come without a folder path: find it with the KB's search
            find(kb, title)
        name, viewer = open_doc(title)
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
            'title': name, 'source': source, 'file': file, 'pages': texts, 'repaired': repaired,
            'alt': {str(k): v for k, v in alt.items()},
            'garbled_pages': [i + 1 for i, t in enumerate(texts) if looks_garbled(t)],
        }


if __name__ == '__main__':  # self-check of the pure helpers
    assert parse_pages('1-3,7,99', 10) == [1, 2, 3, 7]
    assert parse_pages('', 3) == [1, 2, 3]
    assert looks_garbled('机' * 50 + '的')
    assert looks_garbled('2026年中国磁悬浮柔性输送系统研究报告' + '与' * 13)  # a garbled cover page
    assert not looks_garbled('目录 磁悬浮柔性输送系统产品概要 中国市场分析 本土重点企业分析')
    assert not looks_garbled('人形机器人关节模组市场规模预计在二零三零年达到数百亿元，主要驱动力来自产业链降本与应用落地。' * 2)
    print('ok')
