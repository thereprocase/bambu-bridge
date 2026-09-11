import * as Network from "expo-network";
import { useEffect } from "react";
import { AppState } from "react-native";
import { resetEndpointCache } from "../api/endpoint";
import { useLiveStore } from "../store/live";
import { useBridgeStore } from "../store/bridge";
import { useViewingStore } from "./state";
import { viewingNative } from "./native";

/** Network/foreground changes restart reads only. Never replay a printer command. */
export function useConnectionLifecycle() {
  useEffect(() => {
    let previous = "";
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        resetEndpointCache();
        useViewingStore.getState().invalidateNetwork();
        if (AppState.currentState === "active") {
          for (const conn of Object.values(useLiveStore.getState().conns)) { conn.stop(); conn.start(); }
        }
      }, 300);
    };
    const net = Network.addNetworkStateListener(state => {
      const key = JSON.stringify([state.type, state.isConnected, state.isInternetReachable]);
      if (key !== previous) { previous = key; refresh(); }
    });
    const foreground = AppState.addEventListener("change", state => {
      if (state === "active") refresh();
      else {
        if (timer) clearTimeout(timer);
        for (const conn of Object.values(useLiveStore.getState().conns)) conn.stop();
      }
    });
    // A background service must not keep an old credential after changing/disconnecting bridges.
    const config = useBridgeStore.subscribe((next, old) => {
      if (old.bootstrapped && (next.bearer !== old.bearer || next.pairing?.spki !== old.pairing?.spki ||
          next.baseUrl !== old.baseUrl || next.baseUrlLan !== old.baseUrlLan)) {
        void viewingNative?.stopMonitor().catch(() => {});
        refresh();
      }
    });
    return () => { net.remove(); foreground.remove(); config(); if (timer) clearTimeout(timer); };
  }, []);
}
