import { acknowledgePastNativeReviews, nativeQueue } from "../native";
import { request } from "../client";

jest.mock("../client", () => ({ request: jest.fn() }));
jest.mock("../endpoint", () => ({ resolveBaseUrl: jest.fn() }));
jest.mock("../../pairing/native", () => ({}));
jest.mock("../../store/bridge", () => ({ useBridgeStore: { getState: jest.fn() } }));

beforeEach(() => jest.mocked(request).mockReset());

it("paginates within the selected receipt filter", async () => {
  jest.mocked(request).mockResolvedValue({ uploads: [] });
  await nativeQueue(100, "review");
  expect(request).toHaveBeenCalledWith("/native/queue", {
    query: { offset: 100, limit: 100, view: "review" },
  });
});

it("acknowledges past warnings without requesting a start resolution", async () => {
  jest.mocked(request).mockResolvedValue({ acknowledged: 3 });
  await acknowledgePastNativeReviews();
  expect(request).toHaveBeenCalledWith("/native/queue/acknowledge", {
    method: "POST", body: { confirm: "Ignore past review warnings; keep active starts" },
  });
});
