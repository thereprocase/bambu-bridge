import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHeldJog} from '../src/bambu_bridge/static/app/held-jog.js';
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
test('late start after release is stopped and never renewed',async()=>{
 let finish;const stopped=[];const renewed=[];
 const c=createHeldJog({start:()=>new Promise(r=>finish=r),renew:async id=>renewed.push(id),stop:async id=>stopped.push(id)});
 const start=c.start();await c.stop();finish({lease:'one'});await start;await delay(270);
 assert.deepEqual(stopped,['one']);assert.deepEqual(renewed,[]);
});
test('old pending renewal does not block the current generation',async()=>{
 let n=0,finish;const renewed=[];
 const c=createHeldJog({start:async()=>({lease:String(++n)}),renew:id=>{renewed.push(id);return id==='1'?new Promise(r=>finish=r):Promise.resolve();},stop:async()=>{}});
 await c.start();await delay(270);await c.stop();await c.start();await delay(270);
 assert.ok(renewed.includes('2'));finish();await c.stop();
});
