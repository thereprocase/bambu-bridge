/** A host deadman lease; renewals never enqueue movement segments themselves. */
export interface HoldTransport {
  start(): Promise<{lease:string}>;
  renew(lease:string): Promise<unknown>;
  stop(lease:string): Promise<unknown>;
}
export function createHoldLease(transport: HoldTransport, failed: (error:unknown)=>void) {
  let generation=0;
  let lease:string|null=null;
  let timer:ReturnType<typeof setTimeout>|null=null;
  let renewing:number|null=null;
  let held=false;
  const clear=()=>{if(timer!==null)clearTimeout(timer);timer=null;};
  async function stop(){
    generation+=1;held=false;clear();const value=lease;lease=null;
    if(value){try{await transport.stop(value);}catch{/* Server deadman still expires; never replay a move. */}}
  }
  async function renew(current:number){
    if(!held||current!==generation||!lease||renewing===current)return;
    renewing=current;
    try{await transport.renew(lease);}
    catch(error){if(current===generation){failed(error);void stop();}return;}
    finally{if(renewing===current)renewing=null;}
    if(held&&current===generation)timer=setTimeout(()=>void renew(current),250);
  }
  async function start(){
    if(held)return;
    held=true;const current=++generation;
    try{
      const result=await transport.start();
      if(!held||current!==generation){try{await transport.stop(result.lease);}catch{}return;}
      lease=result.lease;
      timer=setTimeout(()=>void renew(current),250);
    }catch(error){if(current===generation){held=false;failed(error);}}
  }
  return {start,stop,get active(){return held;}};
}
