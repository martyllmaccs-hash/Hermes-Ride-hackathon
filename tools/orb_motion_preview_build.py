#!/usr/bin/env python3
"""P4 preview: the real orb (index.html CSS + DOM, orb.js, orb_motion.js) driven by a synthetic motorcycle.

Usage: orb_motion_preview_build.py [plugin_dir] [out_html]
The motion buttons feed RideOrbMotion the same kind of samples a mounted phone would give (portrait mount, iOS sign):
specific force along the bike's down (heavier in a lean: 1/cos(lean)), yaw rate about the leaned axis
(g*sin(lean)/v), a roll rate while leaning in and out, GPS speed 15 m/s. Lean never comes from accelerometer roll.
The 60 Hz sample loop is the preview's own stand-in for DeviceMotion; the page has no such timer.
"""
import os
import re
import sys

plugin = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = sys.argv[2] if len(sys.argv) > 2 else 'orb_preview.html'
static = os.path.join(plugin, 'static')
html = open(os.path.join(static, 'index.html')).read()
lines = html.split('\n')
start = next(i for i, l in enumerate(lines) if 'the orb (orb.js)' in l)
end = next(i for i, l in enumerate(lines) if i > start and 'show_word (debug)' in l)
css = '\n'.join(l for l in lines[start:end] if not l.strip().startswith('#orb{position:fixed'))
dom = re.search(r'<div id="orb" class="orb d-simple s-sleep" aria-hidden="true">.*?\n</div>\n', html, re.S).group(0)
js = '\n'.join(open(os.path.join(static, f)).read() for f in ('orb.js', 'orb_motion.js')).replace('</script>', '<\\/script>')

page = '''<style>
.stage{position:relative;width:100%;max-width:420px;height:360px;background:#000;border-radius:14px;overflow:hidden}
#orb{position:absolute;left:50%;top:50%;width:250px;height:250px;transform:translate(-50%,-50%);pointer-events:none}
.bar{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px;max-width:420px}
.bar b{font-size:12px;color:var(--muted-foreground);width:100%;font-weight:600}
button{font:inherit;font-size:13px;padding:5px 11px;border-radius:999px;border:1px solid var(--border);background:var(--card);color:var(--foreground);cursor:pointer}
button.on{border-color:var(--accent);color:var(--accent)}
small{color:var(--muted-foreground);display:block;max-width:420px}
.cap{position:absolute;left:12px;bottom:8px;color:#777;font:12px ui-monospace,monospace}
''' + css + '''
</style>
<div class="stage">''' + dom + '''<div class="cap" id="cap">sleep</div></div>
<div class="bar" id="sbar"><b>State</b></div>
<div class="bar" id="mbar"><b>Ride (needs an awake state)</b></div>
<small>Real orb.js, orb_motion.js and the page CSS. The ride buttons feed synthetic phone samples: lean from the gyro
and turn rate (the accelerometer just gets heavier in a corner), brake/accelerate along forward, a bump along down.
Sleep is black and still on purpose.</small>
<script>''' + js + '''
const $=id=>document.getElementById(id);
const orb=RideOrb({root:$('orb'),level:$('orbl'),motion:$('orbm')});
const mo=RideOrbMotion({gain:1,gdown:true});orb.setMotion(mo);
const G=9.81,R=Math.PI/180,V=15;
let lean=0,leanTo=0,lon=0,bumpUntil=0,state='sleep',lvl=null,beat=null;
function sample(t){
  const step=Math.max(-0.9,Math.min(0.9,leanTo-lean));lean+=step;          // deg per 16 ms: ~55 deg/s roll-in
  const roll=step/0.016, c=Math.cos(lean*R), bump=t<bumpUntil?0.7*Math.sin((bumpUntil-t)/60*Math.PI):0;
  const up=1/c+bump, f=[0,up,-lon];                         // phone frame: y up the screen, -z forward (into the screen)
  const yaw=G*Math.sin(lean*R)/V/R;                          // deg/s the gyro sees about the leaned axis
  mo.feed({g:[-G*f[0],-G*f[1],-G*f[2]],la:[0,0,0],rot:[0,-yaw,-roll]},t);mo.setSpeed(V,t);orb.motion(t);
  const s=mo.state();$('cap').textContent=state+'  lean '+s.lean.toFixed(0)+'\\u00b0  lon '+s.lon.toFixed(2)+' g  load '+s.load.toFixed(2)+' g';
}
let t0=performance.now();for(let i=0;i<90;i++)sample(i*16);       // settle the slow means
setInterval(()=>sample(performance.now()-t0+1500),16);
function go(s){state=s;orb.show(s);clearInterval(lvl);clearInterval(beat);
  [...$('sbar').querySelectorAll('button')].forEach(b=>b.classList.toggle('on',b.dataset.s===s));
  if(s==='listening'||s==='speaking'){let k=0;lvl=setInterval(()=>{k+=.25;orb.level(-55+30*Math.max(0,Math.sin(k)*Math.sin(k*2.7)+Math.random()*.3),performance.now()-t0+1500)},50)}
  if(s==='thinking'){beat=setInterval(()=>orb.tone(),5000)}}
['sleep','listening','thinking','speaking','reconnecting'].forEach(s=>{const b=document.createElement('button');b.textContent=s;b.dataset.s=s;b.onclick=()=>{window.__stop=1;go(s)};$('sbar').appendChild(b)});
const ride={'lean left 35\\u00b0':()=>{leanTo=-35},'upright':()=>{leanTo=0;lon=0},'lean right 35\\u00b0':()=>{leanTo=35},
  'brake':()=>{lon=-0.6;setTimeout(()=>lon=0,1400)},'accelerate':()=>{lon=0.4;setTimeout(()=>lon=0,1400)},
  'bump':()=>{bumpUntil=performance.now()-t0+1500+120},
  'twisties':()=>{let n=0;const k=setInterval(()=>{leanTo=[30,-30,25,-35,0][n++];if(n>5)clearInterval(k)},1800);leanTo=-25}};
Object.entries(ride).forEach(([k,f])=>{const b=document.createElement('button');b.textContent=k;b.onclick=()=>{window.__stop=1;if(state==='sleep')go('listening');f()};$('mbar').appendChild(b)});
go('listening');
const demo=[()=>{leanTo=30},()=>{leanTo=-30},()=>{leanTo=0;lon=-0.6},()=>{lon=0},()=>{lon=0.4},()=>{lon=0;bumpUntil=performance.now()-t0+1620},()=>go('speaking'),()=>go('listening')];
const hk=decodeURIComponent((location.hash||'').slice(1));      // screenshots: #lean right 35°, #brake, ...
if(hk&&ride[hk]){window.__stop=1;ride[hk]()}
let d=0;(function auto(){if(window.__stop)return;demo[d++%demo.length]();setTimeout(auto,2200)})();
</script>
'''
os.makedirs(os.path.dirname(out), exist_ok=True)
open(out, 'w').write(page)
print('wrote', out, len(page), 'bytes')
