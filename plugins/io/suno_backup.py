#!/usr/bin/env python3
"""Authorized backup of the current user's Suno creations."""
from __future__ import annotations
import argparse, json, os, re, stat, sys, time
from pathlib import Path
from urllib.parse import urlparse
if __package__ in {None, ""}: sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.database import connect_database
ROOT=Path(__file__).resolve().parents[2]; DEFAULT_DB=ROOT/'db/events.db'; DEFAULT_AUTH=ROOT/'config/suno_auth.json'; OUT=ROOT/'vault/media/suno'

def auth_file(path: Path)->Path:
    path=path.expanduser().resolve()
    if not path.is_file(): raise PermissionError(f"Suno auth required: {path}")
    if stat.S_IMODE(path.stat().st_mode)&0o077: raise PermissionError("Suno auth file must be mode 0600")
    state=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(state,dict) or not isinstance(state.get('cookies'),list): raise ValueError("invalid Playwright storage state")
    return path
def register(db: Path):
    with connect_database(db) as c:
        c.execute("INSERT INTO plugin_registry(plugin_name,type,executable_path,icon,is_active) VALUES (?,?,?,?,1) ON CONFLICT(plugin_name) DO UPDATE SET executable_path=excluded.executable_path,is_active=1",('Suno Personal Backup','io',str(Path(__file__).resolve()),'🎵')); c.execute("INSERT INTO event_routes(event_type,target_plugin_name) VALUES (?,?) ON CONFLICT(event_type) DO UPDATE SET target_plugin_name=excluded.target_plugin_name",('SUNO_BACKUP','Suno Personal Backup')); c.commit()
def args():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--register',action='store_true'); p.add_argument('--backup',action='store_true'); p.add_argument('--db',type=Path,default=DEFAULT_DB); p.add_argument('--auth',type=Path,default=DEFAULT_AUTH); p.add_argument('--url',default='https://suno.com/library'); p.add_argument('--output',type=Path,default=OUT); p.add_argument('--delay',type=float,default=1.0); return p.parse_args()
def main():
    a=args(); a.db=a.db.resolve(); register(a.db)
    if a.register and not a.backup: print(json.dumps({'status':'REGISTERED','plugin':'Suno Personal Backup'})); return 0
    if not a.backup: raise SystemExit('use --backup to download your own Suno library')
    auth=auth_file(a.auth); a.output.mkdir(parents=True,exist_ok=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True); context=browser.new_context(storage_state=str(auth)); page=context.new_page(); page.goto(a.url,wait_until='domcontentloaded',timeout=60_000); page.wait_for_timeout(2_000)
        links=page.locator("a[href*='.mp3'],a[href*='.wav'],audio[src],a[download]"); urls=[]
        for i in range(links.count()):
            node=links.nth(i); u=node.get_attribute('href') or node.get_attribute('src');
            if u and re.search(r'\.(mp3|wav|m4a|flac)(?:\?|$)',u,re.I) and u not in urls: urls.append(u)
        if not urls: raise RuntimeError('No audio links found; verify Suno login/session and library URL')
        results=[]
        for i,u in enumerate(urls,1):
            name=Path(urlparse(u).path).stem or f'suno_{i:04d}'; target=a.output/f'{i:04d}_{re.sub(r"[^a-zA-Z0-9_-]+","_",name)[:80]}.mp3'; response=context.request.get(u,timeout=120_000)
            if not response.ok: continue
            target.write_bytes(response.body()); meta={'source_url':u,'filename':target.name,'bytes':target.stat().st_size,'downloaded_at':time.time()}; target.with_suffix('.json').write_text(json.dumps(meta,indent=2),encoding='utf-8'); results.append(meta); time.sleep(max(0,a.delay))
        browser.close()
    print(json.dumps({'status':'COMPLETED','downloaded':len(results),'output':str(a.output)},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
