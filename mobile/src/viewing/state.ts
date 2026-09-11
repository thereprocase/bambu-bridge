import { create } from "zustand";
import { kv } from "../lib/kv";

export const useViewingStore = create<{
  networkRevision: number;
  keepAwake: boolean;
  invalidateNetwork(): void;
  setKeepAwake(value: boolean): void;
}>((set) => ({
  networkRevision: 0,
  keepAwake: kv.getBoolean("viewing.keepAwake") ?? false,
  invalidateNetwork: () => set(s => ({ networkRevision: s.networkRevision + 1 })),
  setKeepAwake: value => { kv.set("viewing.keepAwake", value); set({ keepAwake: value }); },
}));
