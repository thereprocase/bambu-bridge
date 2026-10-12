export type Availability = { available: boolean; reason: string };
export type Feature = "identity" | "core" | "vision" | "airPrint" | "tangle" | "blob" | "sound" |
  "ams" | "drying" | "workLight" | "nozzleSetup" | "calibration" | "autoRecovery" |
  "motion" | "filamentMotion" | "camera";

/** Manufacturer baseline: BambuStudio C12.json and DevPrintOptions.cpp.
 * Identity comes from the device, never its user-editable friendly name.
 * Protocols still under audit remain unavailable even on capable hardware.
 */
export function printerCapabilities(snapshot: any, registeredModel?: string | null) {
  const raw = snapshot?._raw ?? {};
  const modules = Array.isArray(raw.info?.module) ? raw.info.module : [];
  const reported = modules.find((m: any) => m.name === "ota")?.product_name;
  const identity = String(reported || snapshot?.model || registeredModel || "").trim();
  const model = /^(Bambu Lab\s+)?P1S$/i.test(identity) || identity === "C12" ? "P1S" : identity;
  const known = model === "P1S";
  const units = Array.isArray(raw.ams?.ams) ? raw.ams.ams : [];
  const flag = Number(raw.home_flag);
  const support = (bit: number) => Number.isSafeInteger(flag) && raw.home_flag != null && (flag & (1 << bit)) !== 0;
  const yes: Availability = { available: true, reason: "" };
  const unknown: Availability = { available: false, reason: "Availability unconfirmed" };
  const unavailable: Availability = { available: false, reason: model ? `Not available on ${model}` : "Printer model unknown" };
  const verifiedProtocol: Availability = { available: false, reason: "Control support under review" };
  function get(feature: Feature): Availability {
    switch (feature) {
      case "identity": return yes;
      case "core": return known ? yes : unknown;
      case "vision": case "airPrint": return known ? unavailable : unknown;
      case "tangle": return known && support(19) ? yes : raw.home_flag == null ? unknown : known ? unavailable : unknown;
      case "blob": return known && support(25) ? yes : raw.home_flag == null ? unknown : known ? unavailable : unknown;
      case "sound": return known && support(18) ? yes : raw.home_flag == null ? unknown : known ? unavailable : unknown;
      case "ams": return known && (snapshot?.ams?.present === true || units.length) ? yes : { available: false, reason: "Requires a supported AMS" };
      case "drying": {
        const types = units.map((u: any) => typeof u.info === "string" ? parseInt(u.info, 16) & 15 : null);
        return types.length && types.every((t: any) => t === 1 || t === 2)
          ? { available: false, reason: "Not available on the connected AMS" } : verifiedProtocol;
      }
      case "workLight": return known && Array.isArray(raw.lights_report) && raw.lights_report.some((l: any) => l.node === "work_light") ? yes : raw.lights_report == null ? unknown : known ? unavailable : unknown;
      case "nozzleSetup": return known ? unavailable : verifiedProtocol;
      case "calibration": return verifiedProtocol;
      case "motion": case "filamentMotion": return verifiedProtocol;
      case "camera": return known ? yes : { available: false, reason: "Camera support under review" };
      case "autoRecovery": return known ? yes : unknown;
    }
  }
  return { model, get };
}
