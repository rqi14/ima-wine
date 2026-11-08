#!/usr/bin/env python3
"""MCP server (streamable HTTP) exposing the ima client to agents in other containers.

hermes config.yaml:
  mcp_servers:
    ima:
      url: http://127.0.0.1:8765/mcp
      timeout: 600
"""
import json, os

import anyio
from mcp.server.fastmcp import FastMCP, Image

import ima_client as ima

DOWNLOADS = ima.DOWNLOADS
OCR_PER_CALL = 120  # ~1.5 s/page; keeps one call well inside the 600 s client timeout
mcp = FastMCP('ima', host=os.environ.get('IMA_MCP_HOST', '0.0.0.0'), port=int(os.environ.get('IMA_MCP_PORT', 8765)),
              instructions=(
                  'Tencent ima knowledge bases (personal, shared and subscribed), read through the official '
                  'desktop client. Find documents with ima_search (full text, per knowledge base) or browse with '
                  'ima_list_knowledge_bases / ima_list; read with ima_get_document (full text, page-separated; '
                  'no folder path needed). If a tool says the client is logged out, send the QR image it '
                  'returns to the user to scan with WeChat, then call ima_login_wait.'))


async def run(fn, *args):
    return await anyio.to_thread.run_sync(fn, *args)


def _login_needed():
    """Tool result for a logged-out client: a fresh QR the agent can forward to the user."""
    return ['NOT_LOGGED_IN: the ima client is logged out. Send this QR code to the user and ask them to scan '
            'it with WeChat within ~2 minutes, then call ima_login_wait.',
            Image(data=ima.login_qr(), format='png')]


async def guarded(fn, *args):
    def go():
        with ima.LOCK:
            if not ima.logged_in():
                return _login_needed()
            return fn(*args)
    try:
        return await run(go)
    except ima.ImaError as e:
        return f'ERROR: {e}'


# ---------- login ----------
@mcp.tool()
async def ima_status() -> str:
    """Whether the ima client is up and logged in, and its version."""
    try:
        return json.dumps(await run(ima.status), ensure_ascii=False)
    except ima.ImaError as e:
        return f'ERROR: {e}'


@mcp.tool()
async def ima_login_qr():
    """A fresh WeChat login QR code (PNG). Forward it to the user; it expires in a few minutes."""
    def go():
        if ima.logged_in():
            return 'already logged in'
        return ['Scan with WeChat, then call ima_login_wait.', Image(data=ima.login_qr(), format='png')]
    return await run(go)


@mcp.tool()
async def ima_login_wait(seconds: int = 180) -> str:
    """Block until the user has scanned the QR (or the timeout passes)."""
    ok = await run(ima.wait_login, min(seconds, 600))
    return 'logged in' if ok else 'still logged out: the QR may have expired, call ima_login_qr for a new one'


# ---------- browsing ----------
@mcp.tool()
async def ima_list_knowledge_bases():
    """Names of all knowledge bases in the sidebar: personal, shared with the user, and subscribed."""
    return await guarded(lambda: json.dumps(ima.list_kbs(), ensure_ascii=False))


@mcp.tool()
async def ima_list(kb: str, folders: list[str] | None = None):
    """Items of a knowledge base, or of a folder inside it (folders = path from the root).
    Names may be given as a unique prefix. Returns [{type: folder|document, title, meta}]."""
    def go():
        ima.goto(kb, folders or [])
        return json.dumps(ima.ls(), ensure_ascii=False)
    return await guarded(go)


@mcp.tool()
async def ima_search(kb: str, query: str):
    """Full-text search inside one knowledge base (the client's own search, so it also covers
    subscribed and shared libraries). Returns [{title, snippet}]; pass a title to ima_get_document."""
    return await guarded(lambda: json.dumps(ima.search(kb, query), ensure_ascii=False))


# ---------- reading ----------
def _cache_path(title):
    return os.path.join(DOWNLOADS, '.text', title + '.json')


def _cached(title):
    """Cache file for a title given exactly or as a unique prefix (tools accept prefixes)."""
    d = os.path.dirname(_cache_path('x'))
    names = [n for n in (os.listdir(d) if os.path.isdir(d) else []) if n.endswith('.json')]
    exact = [n for n in names if n[:-5] in (title, title + '.pdf')]
    hits = exact or [n for n in names if n.startswith(title)]
    return os.path.join(d, hits[0]) if len(hits) == 1 else None


def _load(kb, title, folders):
    """Page texts for a document, from the cache or by fetching it through the client once."""
    p = _cached(title)
    if p:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    doc = ima.get_document(kb, title, folders or [])
    os.makedirs(os.path.dirname(_cache_path(doc['title'])), exist_ok=True)
    with open(_cache_path(doc['title']), 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False)
    return doc


@mcp.tool()
async def ima_get_document(kb: str, title: str, folders: list[str] | None = None,
                           pages: str | None = None, max_chars: int = 60000) -> str:
    """Full text of a document in a knowledge base, page-separated ('=== page N/M ===').
    kb/title (prefixes ok) locate it; folders is optional - without it the document is found
    by the knowledge base's search. pages: e.g. '1-20,35' (default all).
    Output stops at max_chars; the footer then says which page to continue from.
    Pages whose embedded fonts have broken maps (text comes out as one repeated character) are
    OCRed automatically and tagged [OCR]; for charts and multi-column slides look at the page with
    ima_render_pages. Fetched once, then served from cache."""
    def go():
        doc = _load(kb, title, folders)
        total = len(doc['pages'])
        wanted = ima.parse_pages(pages, total)
        # garbled pages (broken font maps): OCR the ones asked for, once, and keep the result
        ocr = doc.setdefault('ocr_pages', [])
        todo = [n for n in wanted if n in doc['garbled_pages'] and n not in ocr and doc.get('file')]
        for n in todo[:OCR_PER_CALL]:
            doc['pages'][n - 1] = ima.ocr_page(doc['file'], n)
            ocr.append(n)
        if todo:
            with open(_cached(doc['title']), 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False)
        out, used, last = [], 0, None
        for n in wanted:
            tag = ' [OCR]' if n in ocr else ''
            chunk = f'=== page {n}/{total}{tag} ===\n{doc["pages"][n - 1].strip()}\n'
            if out and used + len(chunk) > max_chars:
                break
            out.append(chunk)
            used += len(chunk)
            last = n
        left = [n for n in doc['garbled_pages'] if n not in ocr]
        head = (f'title: {doc["title"]}\nsource: {doc["source"]}\npages: {total}\n'
                f'ocr_pages: {ocr}' + (f'  (garbled, not OCRed yet: {left})' if left else '') + '\n'
                + ('note: [OCR] pages had broken font maps; OCR misses some headings and mixes columns, '
                   'use ima_render_pages to look at charts/slides\n' if ocr else '') + '\n')
        more = [n for n in wanted if n > (last or 0)]
        tail = f'\n[truncated at max_chars: continue with pages="{more[0]}-{more[-1]}"]' if more else ''
        return head + ''.join(out) + tail
    return await guarded(go)


@mcp.tool()
async def ima_render_pages(title: str, pages: str):
    """Pages of a previously fetched document as images (for garbled_pages, scans, charts).
    At most 6 pages per call."""
    def go():
        p = _cached(title)
        if not p:
            return 'ERROR: fetch it with ima_get_document first'
        with open(p, encoding='utf-8') as f:
            doc = json.load(f)
        if not doc.get('file'):
            return 'ERROR: this document was read off the viewer (no download offered); no page images'
        nums = ima.parse_pages(pages, len(doc['pages']))[:6]
        return [f'pages {nums}'] + [Image(data=jpg, format='jpeg') for jpg in ima.render_pages(doc['file'], nums)]
    return await run(go)


@mcp.tool()
async def ima_screenshot():
    """Screenshot of the client's whole screen, for diagnosing a stuck UI."""
    return Image(data=await run(ima.screenshot), format='png')


if __name__ == '__main__':
    mcp.run(transport='streamable-http')
