#!/usr/bin/env python3
from pathlib import Path
import argparse, hashlib, json, re, time
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--manifest',type=Path,default=ROOT/'vault/media/suno/song_manifest.json'); ap.add_argument('--output',type=Path,default=ROOT/'vault/media/suno'); ap.add_argument('--stop-title',default='Deserted Heart'); ap.add_argument('--stop-count',type=int,default=9); ap.add_argument('--delay',type=float,default=.5); a=ap.parse_args()
    urls=json.loads(a.manifest.read_text()); a.output.mkdir(parents=True,exist_ok=True)
    hashes={hashlib.sha256(p.read_bytes()).hexdigest() for p in a.output.glob('*.mp3')}; title_key=re.sub(r'[^a-z0-9]+','_',a.stop_title.casefold()).strip('_'); hits=len(list(a.output.glob(title_key+'_v*.mp3'))); made=[]
    with sync_playwright() as p:
        b=p.chromium.connect_over_cdp('http://127.0.0.1:9222'); c=b.contexts[0]
        page=c.new_page()
        for n,url in enumerate(urls,1):
            if hits>=a.stop_count: break
            try:
                page.goto(url,wait_until='domcontentloaded',timeout=45_000); page.wait_for_timeout(1200)
                menu=page.get_by_role('button',name=re.compile(r'more options|song options|options',re.I)).last
                menu.click(timeout=5_000,force=True)
                dl=page.get_by_text('Download',exact=True).last; dl.click(timeout=5_000,force=True); page.wait_for_timeout(250)
                mp3=page.get_by_text(re.compile(r'^MP3( Audio)?$',re.I)).last; mp3.click(timeout=5_000,force=True)
                with page.expect_download(timeout=15_000) as info:
                    page.get_by_text('Download Anyway',exact=True).last.click(timeout=5_000,force=True)
                d=info.value; suggested=Path(d.suggested_filename or 'suno_song.mp3'); stem=re.sub(r'[^A-Za-z0-9_-]+','_',suggested.stem).strip('_') or 'suno_song'; suffix=suggested.suffix.lower() or '.mp3'; prior=len(list(a.output.glob(f'*{stem[:50]}*.mp3'))); target=a.output/f'{stem[:80]}_v{prior+1}{suffix}'; tmp=target.with_name('.'+target.name+'.part'); d.save_as(str(tmp)); digest=hashlib.sha256(tmp.read_bytes()).hexdigest()
                if digest in hashes: tmp.unlink(missing_ok=True)
                else:
                    tmp.replace(target); hashes.add(digest); made.append(target.name)
                    if stem.casefold()==title_key: hits+=1
                time.sleep(a.delay)
            except Exception: page.keyboard.press('Escape')
        print(json.dumps({'status':'COMPLETED' if hits>=a.stop_count else 'INCOMPLETE','processed':n if urls else 0,'downloaded':len(made),'stop_title_hits':hits,'files':made,'output':str(a.output)}))
if __name__=='__main__': raise SystemExit(main())
