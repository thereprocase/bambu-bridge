import { canStartStoredPrint, dequeue, enqueue, submitPrint } from "../jobs";
import { request } from "../client";

jest.mock("../client", () => ({ request: jest.fn() }));
beforeEach(() => jest.mocked(request).mockReset());

it("starts a stored file through the current queue contract with physical AMS slots", async () => {
  jest.mocked(request).mockResolvedValueOnce({ id: "queue-1" }).mockResolvedValueOnce({ id: "job-1", state: "queued" });
  await expect(submitPrint({ printer_id: "synthetic", filename: "part.gcode.3mf", ams_mapping: [4] }))
    .resolves.toEqual({ job_id: "job-1", state: "queued" });
  expect(request).toHaveBeenNthCalledWith(1, "/printers/synthetic/queue", { method: "POST",
    body: { file_path: "/part.gcode.3mf", file_name: "part.gcode.3mf", ams_mapping: [4] } });
  expect(request).toHaveBeenNthCalledWith(2, "/queue/queue-1/start", { method: "POST", timeoutMs: 60_000 });
});

it("retains a queued item after an uncertain start without retrying or deleting it", async () => {
  jest.mocked(request).mockResolvedValueOnce({ id: "queue-1" }).mockRejectedValueOnce(new Error("response lost"));
  await expect(submitPrint({ printer_id: "synthetic", filename: "part.gcode.3mf" })).rejects.toThrow("response lost");
  expect(request).toHaveBeenCalledTimes(2);
});

it("uses the current queue add and delete endpoints", async () => {
  jest.mocked(request).mockResolvedValue({ id: "queue-2" });
  await enqueue("synthetic", { filename: "part.gcode.3mf", physical_slot: 1 });
  expect(request).toHaveBeenCalledWith("/printers/synthetic/queue", { method: "POST",
    body: { file_path: "/part.gcode.3mf", file_name: "part.gcode.3mf", ams_mapping: [1] } });
  await dequeue("synthetic", "queue-2");
  expect(request).toHaveBeenLastCalledWith("/queue/queue-2", { method: "DELETE" });
});

it.each(["printing", "preparing", "paused", "failed", "unknown"])("does not start another print in %s", (phase) => {
  expect(canStartStoredPrint({ phase, session: { connected: true } }, true)).toBe(false);
});
it("requires both a fresh socket snapshot and a connected printer", () => {
  const idle = { phase: "idle", session: { connected: true } };
  expect(canStartStoredPrint(idle, true)).toBe(true);
  expect(canStartStoredPrint(idle, false)).toBe(false);
  expect(canStartStoredPrint({ phase: "idle", session: { connected: false } }, true)).toBe(false);
  expect(canStartStoredPrint(null, true)).toBe(false);
});
