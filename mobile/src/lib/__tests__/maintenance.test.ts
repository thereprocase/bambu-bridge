import { maintenanceReady, maintenanceStepAllowed, latestMaintenance, XY_PRESETS, XY_DIRECTIONS } from '../maintenance';
import type { MaintenanceState } from '../../api/control';
const state = (revision: number, session_id = 'one'): MaintenanceState => ({last_requested:{X:128,Y:128,Z:250},position_verified:false,reason:'command',revision,session_id});
test('maintenance requires fresh connected idle homed axes',()=>{
 const now=Date.now();const snapshot={session:{connected:true,last_telemetry_at:new Date(now).toISOString()},_raw:{home_flag:7,gcode_state:'FINISH'}};
 expect(maintenanceReady(snapshot,'Z',now)).toBe(true);
 expect(maintenanceReady({...snapshot,_raw:{...snapshot._raw,gcode_state:'RUNNING'}},'Z',now)).toBe(false);
 expect(maintenanceReady({...snapshot,_raw:{...snapshot._raw,gcode_state:'PAUSE'}},'XY',now)).toBe(false);
 expect(maintenanceReady({...snapshot,_raw:{...snapshot._raw,home_flag:4}},'XY',now)).toBe(false);
 expect(maintenanceReady(snapshot,'Z',now+15001)).toBe(false);
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
