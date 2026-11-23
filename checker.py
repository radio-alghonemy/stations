#!/usr/bin/env python3
"""Daily station-link checker for radio-alghonemy/stations.

Probes every station's current effective stream URL (bundled dataset + existing
overlay applied), finds verified replacements for dead ones via radio-browser,
and regenerates overlay.json + version.json. A station lands in "remove" only
after 3 consecutive daily failures with no working replacement found.
Geo-restricted hosts (e.g. onlinehorizons.net, Egypt-only) are never probed or
auto-replaced: their state is human-managed.
"""
import json
import os, re, sys, time, urllib.request, urllib.parse, http.client
from concurrent.futures import ThreadPoolExecutor

UA = {'User-Agent': 'Mozilla/5.0 (Linux; Android) RadioArab-checker/1.0'}
GEO_HOSTS = ('onlinehorizons.net',)          # Egypt-only: never auto-dead
REMOVE_AFTER = 3                              # consecutive daily failures
TIMEOUT = 8
WORKERS = 60

def norm_url(u):
    m = re.match(r'https?://(?:stream-\d+\.zeno\.fm|stream\.zeno\.fm)(/.*)', u)
    if m: return 'https://stream.zeno.fm' + m.group(1).split('?')[0]
    m = re.match(r'https?://[a-z0-9]+\.radiojar\.com(/[^?]*)', u)
    if m: return 'https://stream.radiojar.com' + m.group(1)
    return u

def probe(u, dyn_meta=None):
    """True if the station looks playable. dyn_meta: metadata url for dynamic dailymotion."""
    try:
        if dyn_meta:
            req = urllib.request.Request(dyn_meta, headers={**UA, 'Referer': 'https://www.maspero.eg/stream/9'})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read(65536).decode('utf-8', 'replace'))
            return 'qualities' in d
        req = urllib.request.Request(norm_url(u), headers={**UA, 'Range': 'bytes=0-2047'})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status < 400
    except urllib.error.HTTPError as e:
        return e.code < 400
    except http.client.BadStatusLine as e:
        return 'ICY' in str(e)   # shoutcast
    except Exception:
        return False

def norm_name(n):
    return re.sub(r'[^\wء-ي]+', '', n.lower())

def find_replacement(name, country_en):
    """Highest-votes radio-browser station with matching name+country that probes alive."""
    try:
        q = urllib.parse.quote(name)
        api = f'https://de1.api.radio-browser.info/json/stations/byname/{q}?limit=10&order=votes&reverse=true'
        with urllib.request.urlopen(urllib.request.Request(api, headers=UA), timeout=10) as r:
            cands = json.loads(r.read().decode())
        nn = norm_name(name)
        for c in cands:
            if norm_name(c.get('name','')) != nn: continue
            if (c.get('countrycode','') or '').lower() != country_en.lower()[:2] and \
               (c.get('country','') or '').lower() != country_en.lower(): continue
            u = c.get('url_resolved') or c.get('url')
            if u and probe(u):
                return u
    except Exception:
        pass
    return None

def main():
    if os.path.exists('all-stations.min.json'):
        stations = json.load(open('all-stations.min.json'))
    else:
        import gzip
        stations = json.load(gzip.open('all-stations.min.json.gz', 'rt', encoding='utf-8'))
    try: overlay = json.load(open('overlay.json'))
    except Exception: overlay = {'version': 0, 'replace': [], 'remove': []}
    try: health = json.load(open('health.json'))
    except Exception: health = {}
    try: version = json.load(open('version.json'))['version']
    except Exception: version = 0

    replaced = {r['id']: r for r in overlay.get('replace', [])}
    # current effective view
    for s in stations:
        r = replaced.get(s['id'])
        if r and r.get('stream_url'):
            s['eff_url'] = r['stream_url']
            s['eff_meta'] = (r.get('resolver') or {}).get('metadata_url')
            s['eff_alts'] = r.get('alt_urls') or []
        else:
            s['eff_url'] = s['url']
            s['eff_meta'] = (s.get('resolver') or {}).get('metadata_url')
            s['eff_alts'] = s.get('alt_urls') or []

    def is_geo(s):
        if any(h in s['eff_url'] for h in GEO_HOSTS): return True
        return False

    probables = [s for s in stations if not is_geo(s)]
    print(f'probing {len(probables)} stations ({len(stations)-len(probables)} geo-managed skipped)', flush=True)

    dead = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(lambda s: probe(s['eff_url'], s.get('eff_meta')), probables))
    for s, ok in zip(probables, results):
        if ok:
            health[s['id']] = 0
        else:
            # one retry before counting
            time.sleep(0.05)
            if probe(s['eff_url'], s.get('eff_meta')):
                health[s['id']] = 0
            else:
                health[s['id']] = health.get(s['id'], 0) + 1
                dead.append(s)
    print(f'dead this run: {len(dead)}', flush=True)

    new_replace = {}
    remove = []
    for s in dead:
        # try existing alternates first
        fixed = None
        for a in s.get('eff_alts') or []:
            if probe(a):
                fixed = a; break
        if fixed is None and not s['eff_url'].startswith('dynamic:'):
            fixed = find_replacement(s['id'].split('|',1)[1], s['id'].split('|',1)[0])
        if fixed:
            new_replace[s['id']] = {'id': s['id'], 'stream_url': fixed,
                                    'alt_urls': [a for a in ([s['eff_url']] + (s.get('eff_alts') or [])) if a != fixed][:3]}
            health[s['id']] = 0
        elif health[s['id']] >= REMOVE_AFTER:
            remove.append(s['id'])

    # keep prior replaces whose URL still differs from bundled and still probes alive
    for s in stations:
        rid = s['id']
        if rid in new_replace or rid in remove: continue
        if rid in replaced and s['eff_url'] != s['url']:
            # keep only if bundled url is still bad or replacement is better; verify replacement alive
            if probe(s['eff_url'], s.get('eff_meta')):
                keep = {'id': rid, 'stream_url': s['eff_url']}
                r = replaced[rid]
                if r.get('alt_urls'): keep['alt_urls'] = r['alt_urls']
                if r.get('resolver'): keep['resolver'] = r['resolver']
                if r.get('codec'): keep['codec'] = r['codec']
                if 'bitrate' in r: keep['bitrate'] = r['bitrate']
                new_replace[rid] = keep

    # persist health
    json.dump(health, open('health.json','w'), ensure_ascii=False, separators=(',',':'))

    new_overlay = {'version': version + 1,
                   'replace': list(new_replace.values()),
                   'remove': remove}
    changed = (json.dumps(sorted(new_overlay['replace'], key=lambda r: r['id']), sort_keys=True)
               != json.dumps(sorted(overlay.get('replace', []), key=lambda r: r['id']), sort_keys=True)
               or sorted(remove) != sorted(overlay.get('remove', [])))
    if not changed:
        print('no changes; overlay stays at version', version)
        return
    json.dump(new_overlay, open('overlay.json','w'), ensure_ascii=False, separators=(',',':'))
    json.dump({'version': version + 1, 'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())},
              open('version.json','w'), separators=(',',':'))
    print(f'overlay v{version+1}: {len(new_replace)} replaces, {len(remove)} removes')

if __name__ == '__main__':
    main()
