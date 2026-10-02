import { printerCapabilities } from "../capabilities";

const p1s = { model: null, _raw: {
  info: { module: [{ name: "ota", product_name: "Bambu Lab P1S" }] },
  home_flag: 23152016,
  ams: { ams: [{ id: "0", info: "1001" }] },
  lights_report: [{ node: "chamber_light", mode: "on" }],
} };
test("live P1S identity works with null root model", () => {
  const c = printerCapabilities(p1s);
  expect(c.model).toBe("P1S");
  expect(c.get("core").available).toBe(true);
  expect(c.get("ams").available).toBe(true);
  expect(c.get("drying")).toEqual({ available: false, reason: "Not available on the connected AMS" });
});
test.each(["vision", "airPrint", "tangle", "blob", "sound", "workLight", "nozzleSetup"] as const)(
  "%s is unavailable on the live P1S", (feature) => {
    expect(printerCapabilities(p1s).get(feature)).toEqual({ available: false, reason: "Not available on P1S" });
  });
test("unknown identity is conservative; friendly names do not establish capability", () => {
  expect(printerCapabilities({ friendly_name: "P1S" }).get("core").available).toBe(false);
  expect(printerCapabilities(null).get("vision").reason).toBe("Availability unconfirmed");
  expect(printerCapabilities(null).get("identity").available).toBe(true);
});
test.each(["P1S", "Bambu Lab P1S", "C12"])("registered model %s works before module data", model => {
  expect(printerCapabilities(null, model).get("core").available).toBe(true);
});
test("device identity overrides registration and switches capability set", () => {
  expect(printerCapabilities({ _raw: { info: { module: [{ name: "ota", product_name: "X1 Carbon" }] } } }, "P1S").model).toBe("X1 Carbon");
});
test("detection capability is independent of enabled state", () => {
  const c = printerCapabilities({ model: "P1S", _raw: { home_flag: 1 << 19 } });
  expect(c.get("tangle").available).toBe(true);
  expect(c.get("blob").available).toBe(false);
});
test("AMS 2 Pro hardware alone does not approve the incorrect drying command", () => {
  expect(printerCapabilities({ model: "P1S", _raw: { ams: { ams: [{ info: "1003" }] } } }).get("drying").reason).toBe("Control support under review");
});
test("calibration remains gated pending wire correction", () => {
  expect(printerCapabilities(p1s).get("calibration").available).toBe(false);
});
test("reported features on another model require a qualified adapter", () => {
  const c = printerCapabilities({ model: "H2D", _raw: { home_flag: (1 << 19) | (1 << 18) | (1 << 25), lights_report: [{node: "work_light"}] } });
  for (const f of ["tangle", "sound", "blob", "workLight", "camera"] as const) expect(c.get(f).available).toBe(false);
});
test("AMS remains present during RFID discovery", () => {
  expect(printerCapabilities({ model: "P1S", ams: {present: true}, _raw: {ams: {ams: []}} }).get("ams").available).toBe(true);
});
test("missing support telemetry remains unconfirmed", () => {
  expect(printerCapabilities({model: "P1S"}).get("tangle").reason).toBe("Availability unconfirmed");
});
test.each(["motion", "filamentMotion"] as const)("%s stays withheld pending validation", feature => {
  expect(printerCapabilities(p1s).get(feature)).toEqual({available: false, reason: "Control support under review"});
});
