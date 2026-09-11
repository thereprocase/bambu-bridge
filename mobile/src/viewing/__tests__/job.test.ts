import { canViewJob } from "../job";

test("completed jobs remain viewable without enabling print controls", () => {
  expect(canViewJob({ phase: "finished", job: { subtask_name: "test" } })).toBe(true);
  expect(canViewJob({ phase: "failed", job: { subtask_name: "test" } })).toBe(true);
  expect(canViewJob({ phase: "idle", job: {} })).toBe(false);
  expect(canViewJob(null)).toBe(false);
  expect(canViewJob({ job: { subtask_name: " " } })).toBe(false);
});
