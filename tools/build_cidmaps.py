#!/usr/bin/env python3
"""Build CID -> Unicode tables for Source Han Sans/Serif (imactl/cidmaps/*.json.gz).

PDFs made with Source Han subsets often carry a broken or partial ToUnicode. The subset keeps the
original CIDs, and the full font maps those CIDs to Unicode - but the numbering differs between
releases (1.x vs 2.x) and between the region subset (CN) and the Pan-CJK (SC) fonts, so we keep one
table per (family, region, release) and let the repair pick the one that decodes to real text.
CIDs are the same across weights, so Regular is enough. Fonts are OFL; the tables are just data.

Run once (needs fontTools); writes ../imactl/cidmaps/. Downloads ~150 MB of fonts to a temp dir.
"""
import gzip, json, os, sys, tempfile, urllib.request
from fontTools.ttLib import TTFont

BASE = 'https://github.com/adobe-fonts/{repo}/raw/{tag}/{path}'
# CID numbering changes between releases, even minor ones (Serif 1.000 -> 1.001 moved 持 from
# CID 19302 to 19301), so every release gets its own table; identical ones are deduplicated.
TAGS = {
    'Sans': ('source-han-sans', ['1.001R', '1.002R', '1.003R', '1.004R', '2.000R', '2.001R', '2.002R',
                                 '2.003R', '2.004R', '2.005R']),
    'Serif': ('source-han-serif', ['1.000R', '1.001R', '2.000R', '2.001R', '2.002R', '2.003R']),
}
REGIONS = {  # the CN region subset keeps the Pan-CJK CIDs of its glyphs; SC is the full set
    'CN': 'SubsetOTF/CN/SourceHan{fam}CN-Regular.otf',
    'SC': 'OTF/SimplifiedChinese/SourceHan{fam}SC-Regular.otf',
}
SOURCES = {f'SourceHan{fam}{reg}-{tag[:-1]}': (repo, tag, path.format(fam=fam))
           for fam, (repo, tags) in TAGS.items() for tag in tags for reg, path in REGIONS.items()}


def gsub_pairs(tt):
    """(input glyph, output glyph) of every single / alternate substitution in GSUB."""
    if 'GSUB' not in tt:
        return
    for lookup in tt['GSUB'].table.LookupList.Lookup:
        for st in lookup.SubTable:
            if lookup.LookupType == 7:  # extension: unwrap
                st = st.ExtSubTable
            if getattr(st, 'mapping', None) and st.LookupType == 1:
                yield from st.mapping.items()
            elif getattr(st, 'alternates', None) and st.LookupType == 3:
                for src, alts in st.alternates.items():
                    for a in alts:
                        yield src, a


def table(path):
    tt = TTFont(path)
    out = {}
    for cp, gname in sorted(tt.getBestCmap().items()):
        if not gname.startswith('cid'):
            continue
        cid = int(gname[3:])
        # several code points can share a glyph: prefer unified ideographs over compatibility forms
        if cid not in out or (0x4E00 <= cp <= 0x9FFF and not 0x4E00 <= out[cid] <= 0x9FFF):
            out[cid] = cp
    # Glyphs with no code point of their own (regional / vertical / alternate forms) are reached
    # through GSUB from an encoded glyph; a PDF subset can contain them directly (e.g. CID 19302,
    # a form of 持, in Source Han Serif CN 1.x). Follow the substitutions backwards, until stable.
    pairs = list(gsub_pairs(tt))
    added = True
    while added:
        added = False
        for src, dst in pairs:
            if src.startswith('cid') and dst.startswith('cid'):
                s, d = int(src[3:]), int(dst[3:])
                if s in out and d not in out:
                    out[d] = out[s]
                    added = True
    return out


def main():
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'imactl', 'cidmaps')
    os.makedirs(dest, exist_ok=True)
    for old in os.listdir(dest):
        if old.endswith('.json.gz'):
            os.remove(os.path.join(dest, old))
    kept = {}  # table content -> name, to drop releases that did not renumber
    with tempfile.TemporaryDirectory() as tmp:
        for name, (repo, tag, path) in SOURCES.items():
            url = BASE.format(repo=repo, tag=tag, path=path)
            font = os.path.join(tmp, name + '.otf')
            try:
                urllib.request.urlretrieve(url, font)
                t = table(font)
            except Exception as e:
                print(f'{name}: FAILED {url}: {e}', file=sys.stderr)
                continue
            finally:
                if os.path.exists(font):
                    os.remove(font)
            key = json.dumps(t, sort_keys=True)
            if key in kept:
                print(f'{name}: same numbering as {kept[key]}, skipped')
                continue
            kept[key] = name
            with gzip.open(os.path.join(dest, name + '.json.gz'), 'wt', encoding='utf-8') as f:
                json.dump(t, f, separators=(',', ':'))
            print(f'{name}: {len(t)} CIDs from {tag}/{path}')


if __name__ == '__main__':
    main()
