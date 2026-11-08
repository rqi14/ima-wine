#!/usr/bin/env python3
"""imactl - command line over ima_client, for humans debugging the container.
Agents should use the MCP server (server.py) instead."""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import ima_client as ima


def out(x):
    print(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, indent=1))


def save(png, path):
    with open(path, 'wb') as f:
        f.write(png)
    return os.path.abspath(path)


def main():
    ap = argparse.ArgumentParser(prog='imactl')
    sp = ap.add_subparsers(dest='cmd', required=True)
    sp.add_parser('status')
    sp.add_parser('qr').add_argument('-o', default='ima-login-qr.png')
    sp.add_parser('screenshot').add_argument('-o', default='ima-screen.png')
    sp.add_parser('close')
    sp.add_parser('kbs')
    p = sp.add_parser('ls'); p.add_argument('kb'); p.add_argument('folders', nargs='*')
    p = sp.add_parser('get', help='full text of a document')
    p.add_argument('kb'); p.add_argument('path', nargs='+', help='folders..., title')
    a = ap.parse_args()
    try:
        if a.cmd == 'status':
            out(ima.status())
        elif a.cmd == 'qr':
            out(save(ima.login_qr(), a.o))
        elif a.cmd == 'screenshot':
            out(save(ima.screenshot(), a.o))
        elif a.cmd == 'close':
            ima.close()
        elif a.cmd == 'kbs':
            out(ima.list_kbs())
        elif a.cmd == 'ls':
            ima.goto(a.kb, a.folders)
            for i in ima.ls()['items']:
                print(('F  ' if i['type'] == 'folder' else 'D  ') + i['title'] + f'    [{i["meta"]}]')
        elif a.cmd == 'get':
            *folders, title = a.path
            doc = ima.get_document(a.kb, title, folders)
            print(f"# {doc['title']}  source={doc['source']} file={doc['file']} garbled={doc['garbled_pages']}")
            for n, t in enumerate(doc['pages'], 1):
                print(f'=== page {n}/{len(doc["pages"])} ===\n{t.strip()}')
    except ima.ImaError as e:
        sys.exit(f'imactl: {e}')


if __name__ == '__main__':
    main()
