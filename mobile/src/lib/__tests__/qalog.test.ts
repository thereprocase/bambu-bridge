import { qaLog, registerQaSecret } from "../qalog";

afterEach(() => jest.restoreAllMocks());
it("redacts nested credentials, URL errors, addresses and known secrets", () => {
  const log = jest.spyOn(console, "log").mockImplementation(() => undefined);
  registerQaSecret("synthetic-secret-value");
  qaLog("ws.state", { state: "error", nested: [{ Authorization: "Bearer hidden-token", accessCode: "87654321",
    payload: "http://hidden.invalid:8080/status?token=private", custom: "synthetic-secret-value" }],
    detail: "private hostname", address: "192.0.2.12", numeric: 12 });
  const output = JSON.stringify(log.mock.calls);
  for (const value of ["hidden-token", "87654321", "hidden.invalid", "private hostname", "192.0.2.12", "synthetic-secret-value"]) expect(output).not.toContain(value);
  expect(output).toContain('numeric'); expect(output).toContain('redacted');
});
it("does not crash the app on cyclic diagnostic payloads", () => {
  jest.spyOn(console, "log").mockImplementation(() => undefined);
  const cycle: Record<string, unknown> = {}; cycle.self = cycle;
  expect(() => qaLog("viz.timings", cycle)).not.toThrow();
});
