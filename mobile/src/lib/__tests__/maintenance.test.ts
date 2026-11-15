import { maintenanceReady, maintenanceBlocker, maintenanceDisabledReason, MAINTENANCE_STEPS, maintenanceStepAllowed, latestMaintenance, XY_PRESETS, XY_DIRECTIONS } from '../maintenance';
import type { MaintenanceState } from '../../api/control';
const state = (revision: number, session_id = 'one'): MaintenanceState => ({last_requested:{X:128,Y:128,Z:250},position_verified:false,reason:'command',revision,session_id});
const idle=(extra:Record<string,any>={})=>({session:{connected:true,last_telemetry_at:new Date().toISOString()},_raw:{home_flag:7,gcode_state:'FINISH',...extra}});
test('maintenance requires a live link and a connected idle homed printer',()=>{
 const snapshot=idle();
 expect(maintenanceReady(snapshot,'Z','open')).toBe(true);
 expect(maintenanceReady(idle({gcode_state:'RUNNING'}),'Z','open')).toBe(false);
 expect(maintenanceReady(idle({gcode_state:'PAUSE'}),'XY','open')).toBe(false);
 expect(maintenanceReady(idle({home_flag:4}),'XY','open')).toBe(false);
 expect(maintenanceReady(idle({home_flag:3}),'Z','open')).toBe(false);
 expect(maintenanceReady({...snapshot,session:{connected:false}},'Z','open')).toBe(false);
 for(const link of ['closed','connecting','error'] as const) expect(maintenanceReady(snapshot,'Z',link)).toBe(false);
});
test('F02: status age alone never disables movement',()=>{
 const old={...idle(),session:{connected:true,last_telemetry_at:new Date(Date.now()-27000).toISOString()}};
 expect(maintenanceReady(old,'Z','open')).toBe(true);
 expect(maintenanceReady(old,'XY','open')).toBe(true);
 const never={...idle(),session:{connected:true}};
 expect(maintenanceReady(never,'Z','open')).toBe(true);
});
test('F17: the disabled reason names the first blocker, and is null when nothing blocks',()=>{
 expect(maintenanceDisabledReason(idle(),'open',false)).toBeNull();
 expect(maintenanceDisabledReason(idle(),'closed',false)).toBe('Bridge link is down; waiting to reconnect.');
 expect(maintenanceDisabledReason({...idle(),session:{connected:false}},'open',false)).toBe('Printer is offline.');
 expect(maintenanceDisabledReason(null,'open',false)).toBe('Printer is offline.');
 expect(maintenanceDisabledReason(idle({gcode_state:'RUNNING'}),'open',false)).toBe('Printer is busy.');
 expect(maintenanceDisabledReason(idle({home_flag:0}),'open',false)).toBe('Z axis is not homed.');
 expect(maintenanceDisabledReason(idle({home_flag:4}),'open',false)).toBe('X and Y axes are not homed.');
 expect(maintenanceDisabledReason(idle({gcode_state:undefined}),'open',false)).toBe('Waiting for the first printer status report.');
 expect(maintenanceDisabledReason(idle(),'open',true)).toBe('A movement command is in progress.');
 expect(maintenanceDisabledReason(idle({gcode_state:'RUNNING'}),'closed',true)).toBe('Bridge link is down; waiting to reconnect.');
 expect(maintenanceBlocker(idle({home_flag:4}),'Z','open')).toBeNull();
});
test('F03: the step list is the three bridge-accepted sizes',()=>{
 expect([...MAINTENANCE_STEPS]).toEqual([1,10,50]);
 expect(maintenanceStepAllowed(state(1),'Z',-1,25)).toBe(false);
});
test('only newer same-session command references override live state',()=>{
 expect(latestMaintenance(state(3),state(2))?.revision).toBe(3);
 expect(latestMaintenance(state(3),state(4))?.revision).toBe(4);
 expect(latestMaintenance(state(0,'new'),state(4,'old'))?.session_id).toBe('new');
});
test('requested reference is mandatory and bounds prevent overrun',()=>{
 expect(maintenanceStepAllowed(undefined,'Z',1,10)).toBe(false);
 expect(maintenanceStepAllowed(state(1),'Z',1,1)).toBe(false);
 expect(maintenanceStepAllowed(state(1),'Z',-1,50)).toBe(true);
 expect(maintenanceStepAllowed({...state(1),last_requested:{X:null,Y:null,Z:null}},'X',1,10)).toBe(false);
});
test('nine nested XY positions and eight joystick directions are distinct and bounded',()=>{
 expect(XY_PRESETS).toHaveLength(9);expect(XY_DIRECTIONS).toHaveLength(8);
 expect(new Set(XY_PRESETS.map(p=>`${p.x},${p.y}`)).size).toBe(9);
 expect(XY_PRESETS.every(p=>p.x>=0&&p.x<=256&&p.y>=0&&p.y<=256)).toBe(true);
 expect(new Set(XY_DIRECTIONS.map(d=>`${d[2]},${d[3]}`)).size).toBe(8);
});
