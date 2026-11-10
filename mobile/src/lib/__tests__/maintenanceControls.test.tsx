import React from 'react';
import renderer,{act} from 'react-test-renderer';
import { AppState, Text } from 'react-native';
import * as control from '../../api/control';
let mockSnapshot:any;
const mockToast=jest.fn();
jest.mock('@react-navigation/native',()=>({useIsFocused:()=>true}));
jest.mock('../../store/live',()=>({useLiveStore:(select:any)=>select({printers:{P1:{snapshot:mockSnapshot}}})}));
jest.mock('../../components/Toast',()=>({useToastStore:(select:any)=>select({show:mockToast})}));
jest.mock('../../theme/ThemeProvider',()=>({useTheme:()=>{const t=jest.requireActual('../../theme/tokens');return {c:t.light,space:t.space,type:t.typography,radius:t.radius};}}));
jest.mock('../../api/control',()=>({moveBedAbsolute:jest.fn(),moveXYAbsolute:jest.fn(),maintenanceZStep:jest.fn(),maintenanceXYStep:jest.fn(),qualifyHeldJog:jest.fn(),startHeldJog:jest.fn(),renewHeldJog:jest.fn(),stopHeldJog:jest.fn()}));
import { MaintenanceControls } from '../../components/MaintenanceControls';
const api=control as jest.Mocked<typeof control>;
function seed(state='FINISH'){
 mockSnapshot={session:{connected:true,last_telemetry_at:new Date().toISOString()},_raw:{home_flag:7,gcode_state:state},maintenance:{last_requested:{X:128,Y:128,Z:125},position_verified:false,revision:1,session_id:'one',reason:'command',hold:{qualified:true,active:false,phase:'ready',reason:'ready',lease_ms:750,segment_mm:1}}};
}
function response(){return {sent:{},confirmation:'pending' as const,maintenance:mockSnapshot.maintenance};}
function press(root:renderer.ReactTestRenderer,label:string){const node=root.root.findAll(n=>n.props.accessibilityLabel===label&&typeof n.props.onPress==='function')[0];expect(node).toBeTruthy();return node;}
beforeEach(()=>{jest.useFakeTimers();jest.clearAllMocks();Object.defineProperty(AppState,'currentState',{configurable:true,value:'active'});seed();api.moveXYAbsolute.mockResolvedValue(response());api.moveBedAbsolute.mockResolvedValue(response());api.maintenanceZStep.mockResolvedValue(response());api.maintenanceXYStep.mockResolvedValue(response());api.startHeldJog.mockResolvedValue({lease:'a'.repeat(32),lease_ms:750,hold:mockSnapshot.maintenance.hold});api.renewHeldJog.mockResolvedValue({lease_ms:750,hold:mockSnapshot.maintenance.hold});api.stopHeldJog.mockResolvedValue({hold:mockSnapshot.maintenance.hold});});
afterEach(()=>jest.useRealTimers());
test('native presets and eight-direction pad use bounded maintenance endpoints',async()=>{
 let root!:renderer.ReactTestRenderer;await act(async()=>{root=renderer.create(<MaintenanceControls printerId="P1"/>);});
 await act(async()=>{press(root,'Bottom: move bed to Z250 mm').props.onPress();});expect(api.moveBedAbsolute).toHaveBeenCalledWith('P1',250);
 await act(async()=>{press(root,'Halfway back left: X64 Y192 mm').props.onPress();});expect(api.moveXYAbsolute).toHaveBeenCalledWith('P1',64,192);
 await act(async()=>{press(root,'Step back left by selected XY step').props.onPress();});expect(api.maintenanceXYStep).toHaveBeenCalledWith('P1',-1,1,10);
 await act(async()=>{root.unmount();});
});
test('printing disables all native movement actions',async()=>{
 seed('RUNNING');let root!:renderer.ReactTestRenderer;await act(async()=>{root=renderer.create(<MaintenanceControls printerId="P1"/>);});
 expect(press(root,'Bottom: move bed to Z250 mm').props.disabled).toBe(true);
 expect(press(root,'Step back by selected XY step').props.disabled).toBe(true);
 expect(api.moveBedAbsolute).not.toHaveBeenCalled();await act(async()=>{root.unmount();});
});
test('last request does not masquerade as a measured physical position',async()=>{
 let root!:renderer.ReactTestRenderer;await act(async()=>{root=renderer.create(<MaintenanceControls printerId="P1"/>);});
 const strings=root.root.findAllByType(Text).flatMap(n=>n.props.children).filter(value=>typeof value==='string').join(' ');
 expect(strings).toContain('Physical position is unverified');await act(async()=>{root.unmount();});
});

test('short tap steps once; long press holds only and releases without a click step',async()=>{
 let root!:renderer.ReactTestRenderer;await act(async()=>{root=renderer.create(<MaintenanceControls printerId="P1"/>);});
 let button=press(root,'Step back by selected XY step');
 await act(async()=>{button.props.onPressIn();button.props.onPressOut();button.props.onPress();});
 expect(api.maintenanceXYStep).toHaveBeenCalledTimes(1);api.maintenanceXYStep.mockClear();
 button=press(root,'Step back by selected XY step');
 expect(button.props.pressRetentionOffset).toEqual({top:0,bottom:0,left:0,right:0});
 await act(async()=>{button.props.onPressIn();button.props.onLongPress();});
 expect(api.startHeldJog).toHaveBeenCalledWith('P1',{axes:'XY',direction_x:0,direction_y:1});
 await act(async()=>{jest.advanceTimersByTime(250);});expect(api.renewHeldJog).toHaveBeenCalledTimes(1);
 await act(async()=>{button.props.onPressOut();button.props.onPress();});
 expect(api.maintenanceXYStep).not.toHaveBeenCalled();expect(api.stopHeldJog).toHaveBeenCalledTimes(1);
 await act(async()=>{jest.advanceTimersByTime(1000);});expect(api.renewHeldJog).toHaveBeenCalledTimes(1);
 await act(async()=>{root.unmount();});
});

test('background cancels held jogging and rejects delayed long press events',async()=>{
 let changed!:(value:string)=>void;
 const subscription=jest.spyOn(AppState,'addEventListener').mockImplementation((_,callback:any)=>{changed=callback;return{remove:jest.fn()};});
 let root!:renderer.ReactTestRenderer;await act(async()=>{root=renderer.create(<MaintenanceControls printerId="P1"/>);});
 const button=press(root,'Raise bed by selected Z step');
 await act(async()=>{button.props.onPressIn();button.props.onLongPress();});
 expect(api.startHeldJog).toHaveBeenCalledTimes(1);
 await act(async()=>{changed('background');});expect(api.stopHeldJog).toHaveBeenCalledTimes(1);
 await act(async()=>{button.props.onLongPress();});expect(api.startHeldJog).toHaveBeenCalledTimes(1);
 await act(async()=>{root.unmount();});subscription.mockRestore();
});
