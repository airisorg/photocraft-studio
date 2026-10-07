"""Original editable starter designs, rendered by the existing PhotoCraft engine.

Usage: python packaging/web/generate-templates.py /path/to/photocraft-cli
No flattened images or new document model: every shape and text run is a native layer.
"""
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
out = root / 'apps/photocraft-web/templates'
out.mkdir(parents=True, exist_ok=True)
process = subprocess.Popen([str(Path(sys.argv[1]).resolve()), 'serve', '--automation-write-root', str(out)],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

def rpc(method, params):
    process.stdin.write(json.dumps({'id':1,'method':method,'params':params})+'\n')
    process.stdin.flush()
    response=json.loads(process.stdout.readline())
    if not response.get('ok'):
        raise RuntimeError(response)
    return response.get('result')

def cmd(command, params):
    return rpc('engine.execute', {'command':command,'params':params})

def new(title, bg, width=1080, height=1350):
    rpc('doc.new',{'width':width,'height':height,'name':title,'background':'white'})
    cmd('edit.fill',{'color':bg})

def shape(name, rect, fill, kind='rect', **extra):
    cmd('shape.create',{'name':name,'kind':kind,'rect':rect,'fill':fill,**extra})

def text(value, x, y, size, color, weight=600, **extra):
    cmd('type.create',{'name':value.replace('\n',' '),'x':x,'y':y,'text':value,'size':size,
                       'font':'Inter','weight':weight,'color':color,**extra})

def save(slug):
    rpc('doc.save',{'path':f'{slug}.pcraft'})
    rpc('doc.render',{'path':f'{slug}.png','maxSide':512})
    rpc('doc.close',{})

new('Make some noise', '#e5ed60')
shape('Orbit', [610,110,670,670], '#a395d9','ellipse')
shape('Center', [795,295,300,300], '#e5ed60','ellipse')
text('OFF THE GRID / VOL. 01',75,105,26,'#252a20',500,tracking=130)
text('MAKE\nSOME\nNOISE.',65,390,181,'#202620',600,leading=168,tracking=-50)
shape('Rule',[75,1100,930,3],'#252a20')
text('A gathering of independent minds.',75,1170,30,'#252a20',400)
text('FRIDAY 23 OCT      /      DOORS AT 8',75,1260,25,'#252a20',500,tracking=80)
save('noise')

new('Sunday journal', '#f4ede3')
shape('Sun',[545,190,390,390],'#da6b48','ellipse')
shape('Horizon',[70,640,940,250],'#a4b0a0','roundedRect',radii=[300,300,0,0])
shape('Landscape foreground',[70,760,940,170],'#5e705f','roundedRect',radii=[300,0,0,0])
text('THE SLOW LIVING ISSUE',75,104,25,'#433e36',500,tracking=180)
text('Sunday\njournal.',65,340,157,'#433e36',500,leading=153,tracking=-60)
text('A little less rush.\nA little more room.',75,1070,43,'#433e36',400,leading=58)
text('ISSUE 004      /      AUTUMN 2026',75,1260,24,'#433e36',500,tracking=100)
save('sunday')

new('What comes next', '#232137',1920,1080)
shape('Orbit one',[1280,-150,790,790],'#b7a3f3','ellipse')
shape('Orbit two',[1520,300,590,590],'#fa9974','ellipse')
shape('Cutout',[1500,180,240,240],'#232137','ellipse')
text('NORTH STUDIO     /     STRATEGY 2026',110,115,26,'#cdc7e2',500,tracking=130)
text('What\ncomes next.',100,455,175,'#f6f0e7',600,leading=180,tracking=-45)
text('Ideas with a point of view.',115,860,42,'#cdc7e2',400)
shape('Underline',[115,926,420,4],'#fa9974')
text('01 / INTRODUCTION',1600,1010,22,'#f6f0e7',500)
save('next')

new('A different kind of social', '#dfeeea',1080,1080)
shape('Circle',[530,120,600,600],'#f59dc3','ellipse')
text('STUDIO NOTES / 003',70,100,24,'#243c35',500,tracking=130)
text('Less scroll.\nMore soul.',62,380,128,'#243c35',600,leading=135,tracking=-55)
shape('Baseline',[72,748,934,4],'#243c35')
text('Make room for the work\nyou want to make.',72,848,42,'#243c35',400,leading=58)
text('CREATE SOMETHING THAT FEELS LIKE YOU.',72,1000,21,'#243c35',500,tracking=50)
save('soul')

new('Soft form brand board','#eee9f5',1600,1000)
text('SOFT\nFORM',75,350,202,'#453d61',500,leading=183,tracking=-55)
text('OBJECTS FOR A QUIETER EVERYDAY',85,660,22,'#453d61',500,tracking=120)
shape('Ceramic form',[955,110,400,490],'#b3a3ce','roundedRect',radii=[200,200,20,20])
shape('Inner vessel',[1030,60,250,310],'#ded6ec','ellipse')
shape('Swatch one',[85,796,200,120],'#453d61')
shape('Swatch two',[305,796,200,120],'#b3a3ce')
shape('Swatch three',[525,796,200,120],'#d5dca3')
text('BRAND EXPLORATION\nTYPE / COLOR / FORM',1030,845,28,'#453d61',500,leading=40)
save('softform')

new('After hours','#242a26')
shape('Amber moon',[510,180,740,740],'#ed8e47','ellipse')
shape('Eclipse',[580,135,595,595],'#242a26','ellipse')
text('LISTENING ROOM / EVERY SATURDAY',70,100,24,'#e9e3d2',500,tracking=100)
text('AFTER\nHOURS',65,540,176,'#e9e3d2',600,leading=170,tracking=-60)
shape('Line',[75,1110,930,3],'#e9e3d2')
text('Good music. Better company.',75,1190,37,'#e9e3d2',400)
text('21:00—LATE     /     COME AS YOU ARE',75,1280,24,'#ed8e47',500,tracking=80)
save('afterhours')

process.stdin.close()
if process.wait(timeout=20)!=0:
    raise SystemExit('PhotoCraft template generation failed')
print('Generated six native, layered templates and engine-rendered previews.')
