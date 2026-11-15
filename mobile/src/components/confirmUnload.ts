import { Alert } from "react-native";

/** Ask before unloading: the printer heats the nozzle and retracts the loaded
 * filament, which takes about a minute and cannot be cancelled from the app.
 * Same wording as the web dashboard. */
export function confirmUnload(unload: () => void): void {
  Alert.alert(
    "Unload filament?",
    "The nozzle heats up to release the filament — it will get hot.",
    [
      { text: "Cancel", style: "cancel" },
      { text: "Unload", onPress: unload },
    ],
  );
}
