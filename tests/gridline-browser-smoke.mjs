// Optional real-Chromium smoke test. All API/WS data is synthetic; no printer
// commands or credentials are used. Run: node tests/gridline-browser-smoke.mjs
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const exec = promisify(execFile);
const root = fileURLToPath(new URL('../src/bambu_bridge/static/app/', import.meta.url));
const harness = `<script>
const params=new URLSearchParams(location.search);
localStorage.setItem('bbl.prefs',JSON.stringify({theme:params.get('theme')||'light'}));
localStorage.setItem('bbl.tourSeen','1');
const errors=[];addEventListener('error',e=>errors.push(e.message));
const originalError=console.error;console.error=(...args)=>{errors.push(args.map(String).join(' '));originalError(...args)};
let activeSockets=0;
window.WebSocket=class {
 static OPEN=1;readyState=1;
 constructor(){activeSockets++;setTimeout(()=>{if(this.readyState!==1)return;this.onopen?.();this.onmessage?.({data:JSON.stringify({type:'snapshot',data:{
   printer_id:'qa',friendly_name:'QA printer',phase:'printing',
   session:{connected:true,last_telemetry_at:new Date().toISOString()},
   _raw:{gcode_state:'RUNNING',layer_num:12},job:{layer_num:12,layer_count:30,percent:40},
   temps:{},fans:{},ams:{slots:[]},hms:[]
 }})})},50)}
 send(){}close(){if(this.readyState===1)activeSockets--;this.readyState=3}
};
if(location.hash==='#/queue'){
 setTimeout(()=>location.hash='#/controls',300);
 setTimeout(()=>location.hash='#/queue',650);
}
setTimeout(async()=>{
 await document.fonts.ready;
 const strip=document.querySelector('#bridge-strip');
 const result={errors,activeSockets,overflow:document.documentElement.scrollWidth>innerWidth,
   strip:strip?.textContent,queue:!!document.querySelector('a[href="#/queue"]'),
   main:document.querySelector('#app')?.textContent?.slice(0,120),
   fonts:document.fonts.check('16px "IBM Plex Sans"'),
   square:[...document.querySelectorAll('.card,.btn')].every(e=>getComputedStyle(e).borderRadius==='0px')};
 const out=document.createElement('pre');out.id='qc-result';out.textContent=JSON.stringify(result);out.hidden=true;document.body.append(out);
},1400);
</script>`;
const server = createServer(async (req,res) => {
 const path = new URL(req.url,'http://localhost').pathname;
 res.setHeader('Cache-Control','no-store');
 if(path==='/app/session'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({authentication:'tailscale'}));return}
 if(path.startsWith('/api/')){
   const data=path.endsWith('/printers')?[{printer_id:'qa',friendly_name:'QA printer'}]
     :path.includes('/native/queue')?{uploads:[],has_more:false,review_count:0,acknowledgeable_count:0}
       :[];
   res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return;
 }
 try {
   const name=path==='/app/'?'index.html':path.replace(/^\/app\//,'');
   if(name.includes('..')||!path.startsWith('/app/'))throw Error('invalid path');
   let content=await readFile(root+name);
   res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.ttf')?'font/ttf':'text/html');
   if(name==='index.html')content=content.toString().replace('<head>','<head>'+harness);
   res.end(content);
 }catch{res.writeHead(404);res.end()}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try {
 for(const [width,theme,route] of [[320,'light','queue'],[360,'light','queue'],[390,'dark','queue'],[1280,'light','queue'],[390,'light','']]){
   const {stdout}=await exec('chromium',['--headless','--no-sandbox','--disable-gpu','--no-proxy-server',
     '--dump-dom','--virtual-time-budget=2500',`--window-size=${width},900`,
     `--screenshot=/tmp/gridline-web-${width}-${theme}-${route||'status'}.png`,
     `http://127.0.0.1:${server.address().port}/app/?theme=${theme}#/${route}`],{maxBuffer:4*1024*1024});
   const match=stdout.match(/<pre id="qc-result" hidden="">([^<]+)<\/pre>/);
   assert.ok(match,'browser produced QC report');
   const result=JSON.parse(match[1].replaceAll('&amp;','&'));
   console.log({width,theme,route,...result});
   assert.deepEqual(result.errors,[]);
   assert.equal(result.activeSockets,1,'route switches must not leak status streams');
   assert.equal(result.overflow,false);
   assert.equal(result.queue,true);
   assert.equal(result.fonts,true);
   assert.equal(result.square,true);
   assert.match(result.strip,/Bridge · Connected/);
   assert.match(result.strip,/Printer · Printing/);
   if(route==='queue')assert.match(result.main,/Bridge queue/);
 }
} finally {server.close();}
