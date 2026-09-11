import { NativeModules, PermissionsAndroid, Platform } from "react-native";
import { resolveEndpoint, otherUrlFor } from "../api/endpoint";
import { useBridgeStore } from "../store/bridge";

interface MonitorState { running: boolean; printer: string; state: string; alertsAllowed: boolean; batteryRestricted: boolean }
interface ViewingModule {
  screenOptions(rotate: boolean, awake: boolean): void;
  startMonitor(config: string): Promise<void>;
  stopMonitor(): Promise<void>;
  monitorStatus(): Promise<MonitorState>;
  batterySettings(): void;
}
export const viewingNative = NativeModules.BridgeViewing as ViewingModule;

export async function viewingConfig(printer: string) {
  const route = await resolveEndpoint();
  const { bearer, pairing } = useBridgeStore.getState();
  if (!route.url || !bearer) throw new Error("Configure the bridge first.");
  return { base: route.url, alternate: otherUrlFor(route.url), bearer, printer,
    pairedBase: pairing?.baseUrl, pin: pairing?.spki, remote: pairing?.remoteUrl };
}

export async function startMonitoring(printer: string): Promise<void> {
  if (Platform.OS === "android" && Number(Platform.Version) >= 33) {
    const granted = await PermissionsAndroid.request(PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS);
    if (granted !== PermissionsAndroid.RESULTS.GRANTED)
      throw new Error("Allow notifications in Android Settings to receive print alerts.");
  }
  await viewingNative.startMonitor(JSON.stringify(await viewingConfig(printer)));
}
