import { Alert } from "react-native";

let inFlight = false;

/** Ask before unloading: the printer heats the nozzle and retracts the loaded
 * filament, which takes about a minute and cannot be cancelled from the app.
 * Wording follows the web dashboard. While one unload is being sent, another
 * tap is ignored so a slow connection cannot queue a second unload. */
export function confirmUnload(unload: () => Promise<unknown>, loaded: string | null = null): void {
  if (inFlight) return;
  Alert.alert(
    "Unload filament?",
    `${loaded ? `Unloads ${loaded}. ` : ""}The nozzle heats up to release the filament — it will get hot.`,
    [
      { text: "Cancel", style: "cancel" },
      {
        text: "Unload",
        onPress: () => {
          if (inFlight) return;
          inFlight = true;
          void unload().finally(() => { inFlight = false; });
        },
      },
    ],
  );
}
