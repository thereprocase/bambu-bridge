import type { View } from "./snapshot";

/** Both AMS screens use validated snapshot readings, never humidity levels. */
export function amsEnvironmentLabel(
  unit: View["amsUnits"][number], index: number, lastKnown: boolean,
): string {
  const humidity = unit.humidityPct == null ? "Humidity N/A" : `${unit.humidityPct}% RH`;
  const temperature = unit.temperatureC == null
    ? "Temperature unavailable" : `${unit.temperatureC.toFixed(1)} °C`;
  return `${lastKnown ? "Last known · " : ""}AMS ${Number(unit.id) + 1 || index + 1} · ${humidity} · ${temperature}`;
}
