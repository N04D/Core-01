#!/usr/bin/env python3
"""Authorized backup of the current user's Suno creations."""
from __future__ import annotations
import argparse, hashlib, json, os, re, stat, sys, time
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
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--register',action='store_true'); p.add_argument('--backup',action='store_true'); p.add_argument('--db',type=Path,default=DEFAULT_DB); p.add_argument('--auth',type=Path,default=DEFAULT_AUTH); p.add_argument('--cdp-url',default=os.getenv('SUNO_CDP_URL')); p.add_argument('--url',default='https://suno.com/library'); p.add_argument('--output',type=Path,default=OUT); p.add_argument('--delay',type=float,default=1.0); p.add_argument('--limit',type=int,default=0,help='Maximum songs per run; 0 means all'); p.add_argument('--start',type=int,default=1,help='1-based menu number to start from'); p.add_argument('--title',help='Only process the tab/list whose song title matches this text'); p.add_argument('--max-pages',type=int,default=16,help='Hard upper bound for library pages (default: 16)'); p.add_argument('--stop-title',help='Stop after this title has been encountered the requested number of times'); p.add_argument('--stop-title-count',type=int,default=9,help='Number of stop-title encounters before ending (default: 9)'); return p.parse_args()
def main():
    a=args(); a.db=a.db.resolve(); register(a.db)
    if a.register and not a.backup: print(json.dumps({'status':'REGISTERED','plugin':'Suno Personal Backup'})); return 0
    if not a.backup: raise SystemExit('use --backup to download your own Suno library')
    if not a.cdp_url: auth=auth_file(a.auth)
    a.output.mkdir(parents=True,exist_ok=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        owns_browser = not bool(a.cdp_url)
        if a.cdp_url:
            browser=p.chromium.connect_over_cdp(a.cdp_url); context=browser.contexts[0]; pages=[x for x in context.pages if 'suno.com' in x.url]
            # CDP may expose multiple Suno tabs. Prefer the tab that actually
            # contains the largest rendered set of song menus.
            page=max(pages, key=lambda x: (x.get_by_text(a.title, exact=True).count() if a.title else 0, x.get_by_role('button',name=re.compile(r'more options|song options|options',re.I)).count()), default=None) or context.new_page()
            if '/library' not in page.url and not (a.title and page.get_by_text(a.title, exact=True).count()):
                # The account page contains a Library tab implemented as an
                # SPA route; use it first so all of the user's creations are
                # rendered instead of only the profile's featured songs.
                library_link=page.get_by_role('link', name=re.compile(r'^library$', re.I))
                if library_link.count() and not library_link.first.get_attribute('data-active'):
                    library_link.first.click(timeout=5_000, force=True); page.wait_for_timeout(2_000)
                if '/library' not in page.url:
                    page.goto(a.url,wait_until='domcontentloaded',timeout=60_000)
        else:
            browser=p.chromium.launch(headless=True); context=browser.new_context(storage_state=str(auth)); page=context.new_page(); page.goto(a.url,wait_until='domcontentloaded',timeout=60_000)
        page.wait_for_timeout(2_000)
        body_text=page.locator('body').inner_text(timeout=5000).casefold()
        if 'log in' in body_text or 'join suno' in body_text: raise PermissionError('Suno session is not authenticated on the selected library page')
        # Do not collect or download raw audio URLs from the page.  Suno can
        # expose public/sample CDN links (for example ``sil-100.mp3``) which
        # are not one of the account owner's creations.  A backup is accepted
        # only when it was initiated through the per-song menu below.
        # Suno paginates the library (the page number is an input, with a
        # disabled Previous/Next button at the boundaries). Process every
        # page; within each page scroll enough to render all song cards.
        downloaded=[]; seen_hashes=set(); pages_processed=0; stop_hits=0; stop_reached=False
        for existing in a.output.glob('*.mp3'):
            try:
                seen_hashes.add(hashlib.sha256(existing.read_bytes()).hexdigest())
                if a.stop_title and existing.stem.casefold().startswith(re.sub(r'[^a-zA-Z0-9_-]+','_',a.stop_title).strip('_').casefold() + '_v'):
                    stop_hits += 1
            except OSError: pass
        if a.stop_title and stop_hits >= max(1, a.stop_title_count): stop_reached=True
        def process_page(page, page_no):
            nonlocal stop_hits, stop_reached
            if a.title:
                # Suno exposes each song as an aria-labelled group; scope the
                # menu buttons to the requested title so unrelated songs are
                # never downloaded.
                groups=page.locator('[role="group"][aria-label="'+a.title.replace('"','')+'"]')
                menus=groups.get_by_role('button',name=re.compile(r'more options|song options|options',re.I))
            else:
                menus=page.get_by_role('button',name=re.compile(r'more options|song options|options',re.I))
            total_menus=menus.count(); max_menus=min(total_menus,a.limit) if a.limit and page_no == 1 else total_menus
            for index in range(max_menus):
                global_index=(page_no-1)*100+index+1
                if global_index < max(1, a.start): continue
                try:
                    menus.nth(index).click(timeout=3_000)
                    download=page.get_by_role('menuitem',name=re.compile(r'^download$',re.I))
                    if not download.count(): download=page.get_by_text(re.compile(r'^download$',re.I))
                    download.first.click(timeout=3_000); page.wait_for_timeout(300)
                    mp3=page.get_by_role('menuitem',name=re.compile(r'^mp3(?: audio)?$',re.I))
                    if not mp3.count(): mp3=page.get_by_text(re.compile(r'^mp3(?: audio)?$',re.I))
                    if mp3.count(): mp3.first.click(timeout=3_000)
                    with page.expect_download(timeout=8_000) as info:
                        final=page.get_by_role('menuitem',name=re.compile(r'download anyway|download',re.I))
                        if not final.count(): final=page.get_by_text(re.compile(r'download anyway|download',re.I))
                        final.last.click(timeout=3_000)
                    d=info.value
                    suggested=Path(d.suggested_filename or 'suno_song.mp3')
                    stem=re.sub(r'[^a-zA-Z0-9_-]+','_',suggested.stem).strip('_') or 'suno_song'
                    if a.stop_title and stem.casefold() == re.sub(r'[^a-zA-Z0-9_-]+','_',a.stop_title).strip('_').casefold():
                        stop_hits += 1
                    suffix=suggested.suffix.lower() if suggested.suffix else '.mp3'
                    prior=len(list(a.output.glob(f'*{stem[:50]}*.mp3'))); version=prior + 1
                    target=a.output/f'{stem[:80]}_v{version}{suffix}'
                    while target.exists(): version += 1; target=a.output/f'{stem[:80]}_v{version}{suffix}'
                    temp=target.with_name(f'.{target.name}.part'); d.save_as(str(temp))
                    digest=hashlib.sha256(temp.read_bytes()).hexdigest()
                    if digest in seen_hashes: temp.unlink(missing_ok=True)
                    else: temp.replace(target); seen_hashes.add(digest); downloaded.append(target.name)
                    time.sleep(max(0,a.delay))
                    if a.stop_title and stop_hits >= max(1, a.stop_title_count):
                        stop_reached=True; break
                except Exception: page.keyboard.press('Escape')
        for page_no in range(1, max(1, a.max_pages) + 1):
            if stop_reached: break
            for _ in range(12):
                scroller=page.locator('[class*="clip-browser-list-scroller"]')
                if scroller.count():
                    scroller.first.evaluate('(el) => { el.scrollTop = el.scrollHeight; }')
                page.mouse.wheel(0, 2200); page.wait_for_timeout(900)
            process_page(page, page_no)
            pages_processed=page_no
            if stop_reached: break
            if a.limit: break
            page_input=page.locator('input[placeholder="#"]')
            if not page_input.count(): break
            next_button=page_input.locator('xpath=following-sibling::button[1]')
            if not next_button.count() or next_button.is_disabled(): break
            # The arrow can be visually covered by the virtualized list. Set
            # the controlled page input directly and submit it, which is more
            # reliable than a pointer click and preserves the authenticated SPA.
            target_page=page_no + 1
            page_input.fill(str(target_page)); page_input.press('Enter'); page.wait_for_timeout(1_500)
        if downloaded:
            print(json.dumps({'status':'COMPLETED','downloaded':len(downloaded),'pages_processed':pages_processed,'stop_title_hits':stop_hits,'output':str(a.output),'files':downloaded},ensure_ascii=False));
            if owns_browser: browser.close()
            return 0
        raise RuntimeError(
            'No account-owned Suno songs were downloaded through the per-song '
            'menu; refusing to use public/sample audio URLs'
        )
if __name__=='__main__': raise SystemExit(main())
