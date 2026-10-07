"""One deliberate guest journey on a deployed URL; no crawl, load test or account seeding.

Creates a synthetic document only in this disposable browser, draws, exports PNG, imports
that download and records pixels, renderer, resource timing and a screenshot.
"""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import sync_playwright

url = sys.argv[1]
if urlparse(url).scheme != 'https':
    raise SystemExit('Use the HTTPS URL returned by Tofu')
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get('PHOTOCRAFT_CHROME'), headless=True, args=['--enable-unsafe-webgpu'])
    context = browser.new_context(viewport={'width':1440,'height':960}, accept_downloads=True)
    page = context.new_page()
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(url,wait_until='networkidle',timeout=120000)
    page.wait_for_function('typeof window.photocraftCommand === "function"',timeout=90000)
    page.wait_for_selector('#photocraft_loading', state='detached', timeout=90000)
    page.wait_for_timeout(400)
    page.screenshot(path=str(out/'hosted-workspace.png'))
    def command(method,params=None):
        r=page.evaluate('async ([m,p])=>JSON.parse(await photocraftCommand(m,JSON.stringify(p)))',[method,params or {}])
        assert r['ok'], r
        return r.get('result')
    def execute(name,params):
        return command('engine.execute',{'id':name,'params':params})
    execute('file.new',{'width':960,'height':640,'name':'Hosted acceptance','background':'white'})
    execute('edit.fill',{'color':'#f1edff'})
    execute('shape.create',{'kind':'ellipse','rect':[520,70,300,300],'fill':'#9179ff','name':'Violet circle'})
    execute('shape.create',{'kind':'roundedRect','rect':[90,390,720,110],'radii':24,'fill':'#23222a','name':'Caption card'})
    execute('type.create',{'x':115,'y':465,'text':'Made in PhotoCraft','size':52,'color':'#ffffff'})
    before=command('ui.inspect')
    page.screenshot(path=str(out/'hosted-editor.png'))
    with page.expect_download() as event:
        command('ui.menu.invoke',{'id':'file.export.quickExportAsPng'})
    image_path=out/'hosted-export.png'
    event.value.save_as(image_path)
    with Image.open(image_path) as img:
        assert img.size==(960,640),img.size
        assert img.convert('RGB').getpixel((650,180))==(145,121,255)
    with page.expect_file_chooser() as event:
        command('ui.menu.invoke',{'id':'file.open'})
    event.value.set_files(str(image_path))
    page.wait_for_timeout(700)
    after=command('ui.inspect')
    assert after['document']['width']==960 and after['document']['height']==640
    assert len(before['document']['layers'])==4
    assert not errors,errors
    resources=page.evaluate('performance.getEntriesByType("resource").filter(e=>e.name.endsWith(".wasm")).map(e=>({url:e.name,durationMs:e.duration,encodedBytes:e.encodedBodySize,decodedBytes:e.decodedBodySize}))')
    (out/'hosted-evidence.json').write_text(json.dumps({'url':url,'browser':browser.version,'renderer':before['perf']['timings']['gpuInfo'],
        'beforeLayers':len(before['document']['layers']),'exportSize':[960,640],'reimportSize':[after['document']['width'],after['document']['height']],
        'pageErrors':errors,'wasmResources':resources},indent=2))
    print('Hosted guest edit, PNG export and re-import passed.')
    context.close()
    browser.close()
