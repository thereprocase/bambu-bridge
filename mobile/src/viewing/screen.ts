import { useFocusEffect } from "expo-router";
import { useCallback } from "react";
import { useViewingStore } from "./state";
import { viewingNative } from "./native";

export function useViewingScreen() {
  const awake = useViewingStore(s => s.keepAwake);
  useFocusEffect(useCallback(() => {
    viewingNative?.screenOptions(true, awake);
    return () => viewingNative?.screenOptions(false, false);
  }, [awake]));
}
