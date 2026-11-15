import {createHoldLease} from '../holdLease';
const flush=async()=>{await Promise.resolve();await Promise.resolve();};
beforeEach(()=>jest.useFakeTimers());afterEach(()=>jest.useRealTimers());
test('release stops future renewals and late start response is stopped without renewal',async()=>{
 let complete!:(v:{lease:string})=>void;
 const t={start:jest.fn(()=>new Promise<{lease:string}>(r=>complete=r)),renew:jest.fn(async()=>{}),stop:jest.fn(async()=>{})};
 const ctl=createHoldLease(t,jest.fn());const start=ctl.start();await ctl.stop();complete({lease:'old'});await start;
 jest.advanceTimersByTime(1000);await flush();expect(t.stop).toHaveBeenCalledWith('old');expect(t.renew).not.toHaveBeenCalled();
});
test('an old pending renewal cannot strand the next lease',async()=>{
 let count=0;let release!:(v?:unknown)=>void;
 const t={start:jest.fn(async()=>({lease:String(++count)})),renew:jest.fn((id:string)=>id==='1'?new Promise(r=>release=r):Promise.resolve()),stop:jest.fn(async()=>{})};
 const ctl=createHoldLease(t,jest.fn());await ctl.start();jest.advanceTimersByTime(250);await flush();
 await ctl.stop();await ctl.start();jest.advanceTimersByTime(250);await flush();
 expect(t.renew).toHaveBeenCalledWith('2');release();await flush();jest.advanceTimersByTime(250);await flush();
 expect(t.renew.mock.calls.filter(v=>v[0]==='2').length).toBe(2);await ctl.stop();
});
test('renewal failure stops the lease without replaying a movement start',async()=>{
 const fail=jest.fn();const t={start:jest.fn(async()=>({lease:'one'})),renew:jest.fn(async()=>{throw Error('offline')}),stop:jest.fn(async()=>{})};
 const ctl=createHoldLease(t,fail);await ctl.start();jest.advanceTimersByTime(250);await flush();
 jest.advanceTimersByTime(2000);await flush();expect(t.start).toHaveBeenCalledTimes(1);expect(t.renew).toHaveBeenCalledTimes(1);expect(fail).toHaveBeenCalledTimes(1);expect(ctl.active).toBe(false);
});
