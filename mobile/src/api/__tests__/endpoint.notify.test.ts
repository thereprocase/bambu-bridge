/**
 * Unit tests for the STATEFUL side of src/api/endpoint.ts:
 *
 *   - notifyRequestFailed  → LAN / remote cooldown activation + cache drop
 *   - notifyRequestSucceeded → cooldown clearing + lanAliveUntil + cache flip
 *   - resetEndpointCache   → full module-state reset
 *
 * The existing endpoint.test.ts covers the pure helpers (decidePath,
 * otherUrlFor, sameOrigin, prefixOf). Those are driven via parameters.
 * The notify* functions mutate module-level variables; we observe them via
 * the exported `resolveBaseUrl` (which reads the module state) after mocking
 * away the two external dependencies:
 *
 *   1. expo-network  — the native Wi-Fi probe; we control what it returns.
 *   2. store/bridge  — baseUrl / baseUrlLan / savedNetworks configuration.
 *
 * Test strategy:
 *   - Call resetEndpointCache() in beforeEach so module-level state is clean.
 *   - After a notify call, verify the observable effect: does the NEXT call to
 *     resolveBaseUrl() return the expected URL?
 *   - resolveBaseUrl() caches for 15 s internally; resetEndpointCache() clears
 *     both the cache and the cooldown timestamps, so each test starts cold.
 *
 * Style matches src/api/__tests__/endpoint.test.ts:
 *   - jest.mock() at the top
 *   - `mockStoreState` object mutated in beforeEach / per-test
 *   - URLs as named constants (LAN / REMOTE)
 */

import {
  notifyRequestFailed,
  notifyRequestSucceeded,
  resetEndpointCache,
  resolveBaseUrl,
} from "../endpoint";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const LAN    = "http://192.168.1.10:8080/api/v1";
const REMOTE = "http://100.64.1.2:8080/api/v1";

// Bridge store — controls which URLs are configured.
const mockStoreState: { baseUrl: string; baseUrlLan: string | null; savedNetworks: { prefix: string; addedAt: number }[] } = {
  baseUrl: REMOTE,
  baseUrlLan: LAN,
  savedNetworks: [{ prefix: "192.168.1", addedAt: 0 }],
};

jest.mock("../../store/bridge", () => ({
  useBridgeStore: {
    getState: () => mockStoreState,
  },
}));

// expo-network — controls whether the phone appears to be on the saved LAN.
// Default: on Wi-Fi with the matching /24 prefix → fingerprint match.
const mockNetwork = {
  type: "WIFI" as const,           // matches Network.NetworkStateType.WIFI
  ip: "192.168.1.42",             // matches the saved prefix "192.168.1"
};

jest.mock("expo-network", () => {
  const NetworkStateType = { WIFI: "WIFI", CELLULAR: "CELLULAR", NONE: "NONE", UNKNOWN: "UNKNOWN" };
  return {
    NetworkStateType,
    getNetworkStateAsync: jest.fn(async () => ({ type: NetworkStateType[mockNetwork.type as keyof typeof NetworkStateType] ?? "WIFI" })),
    getIpAddressAsync:    jest.fn(async () => mockNetwork.ip),
  };
});

// ---------------------------------------------------------------------------
// Setup / helpers
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Reset module-level state: cached decision + all cooldowns + lanAliveUntil.
  resetEndpointCache();

  // Reset store to two-URL config.
  mockStoreState.baseUrl    = REMOTE;
  mockStoreState.baseUrlLan = LAN;
  mockStoreState.savedNetworks = [{ prefix: "192.168.1", addedAt: 0 }];

  // Default network state: on the saved LAN Wi-Fi.
  mockNetwork.type = "WIFI";
  mockNetwork.ip   = "192.168.1.42";
});

// ---------------------------------------------------------------------------
// 1. resetEndpointCache — baseline verification
// ---------------------------------------------------------------------------

describe("resetEndpointCache", () => {
  it("after reset, resolveBaseUrl prefers LAN when fingerprint matches", async () => {
    // No prior state pollution. Fingerprint matches → LAN preferred.
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("after reset, no fingerprint and no lanAlive → resolveBaseUrl returns REMOTE", async () => {
    // Simulate a non-matching network (cellular, different subnet).
    mockNetwork.type = "CELLULAR";
    mockNetwork.ip   = "10.0.0.1";
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });
});

// ---------------------------------------------------------------------------
// 2. notifyRequestFailed (LAN)
// ---------------------------------------------------------------------------

describe("notifyRequestFailed — LAN URL", () => {
  it("after LAN failure, next resolveBaseUrl falls back to REMOTE", async () => {
    // Confirm baseline: on the LAN → resolver picks LAN first.
    const before = await resolveBaseUrl();
    expect(before).toBe(LAN);

    // Simulate a LAN network-layer failure.
    notifyRequestFailed(LAN);

    // The cache must have been dropped; the next call re-resolves.
    // Even though fingerprint still matches, LAN is now cooled → REMOTE wins.
    const after = await resolveBaseUrl();
    expect(after).toBe(REMOTE);
  });

  it("notifyRequestFailed with the LAN URL (trailing slash variant) → cools LAN", async () => {
    notifyRequestFailed(LAN + "/");
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });

  it("notifyRequestFailed with the LAN sub-path → cools LAN (same origin match)", async () => {
    notifyRequestFailed(LAN + "/printers");
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });

  it("notifyRequestFailed with REMOTE URL cools REMOTE, next call prefers LAN", async () => {
    // Start on cellular — fingerprint won't match, so resolveBaseUrl would
    // normally pick REMOTE. After REMOTE is cooled, LAN wins via lanAlive/fingerprint
    // if available — but here LAN fingerprint matches → LAN wins anyway.
    notifyRequestFailed(REMOTE);
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("notifyRequestFailed with unknown URL → no cooldown, LAN still preferred", async () => {
    // A URL that matches neither configured origin must not cool either side.
    notifyRequestFailed("http://10.0.0.99:9999/api");
    const url = await resolveBaseUrl();
    // LAN still eligible (fingerprint matches, not cooled).
    expect(url).toBe(LAN);
  });

  it("calling notifyRequestFailed twice (LAN) does not throw and REMOTE still returned", async () => {
    expect(() => {
      notifyRequestFailed(LAN);
      notifyRequestFailed(LAN);
    }).not.toThrow();
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });

  it("LAN failure when no LAN URL configured → no crash, REMOTE returned", async () => {
    mockStoreState.baseUrlLan = null;
    expect(() => notifyRequestFailed(LAN)).not.toThrow();
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });
});

// ---------------------------------------------------------------------------
// 3. notifyRequestFailed — both paths fail (down scenario)
// ---------------------------------------------------------------------------

describe("notifyRequestFailed — both paths failed", () => {
  it("both LAN and REMOTE cooled → resolveBaseUrl still returns a URL (never null)", async () => {
    notifyRequestFailed(LAN);
    notifyRequestFailed(REMOTE);
    // decidePath's both-cooled guard ensures we always get a URL to try.
    const url = await resolveBaseUrl();
    expect(url).toBeTruthy();
    expect(typeof url).toBe("string");
  });

  it("both cooled + fingerprint matches → LAN returned (cooldowns ignored per decidePath)", async () => {
    notifyRequestFailed(LAN);
    notifyRequestFailed(REMOTE);
    // Fingerprint still matches (LAN Wi-Fi detected); both-cooled guard means
    // cooldowns are ignored and fingerprint preference applies → LAN.
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("both cooled + no fingerprint (cellular) → REMOTE returned", async () => {
    mockNetwork.type = "CELLULAR";
    mockNetwork.ip   = "10.0.0.1";
    notifyRequestFailed(LAN);
    notifyRequestFailed(REMOTE);
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });
});

// ---------------------------------------------------------------------------
// 4. notifyRequestSucceeded — cooldown clearing
// ---------------------------------------------------------------------------

describe("notifyRequestSucceeded — LAN URL", () => {
  it("LAN success after LAN failure → LAN preferred again", async () => {
    // Cool down LAN so REMOTE would be picked.
    notifyRequestFailed(LAN);
    const cooled = await resolveBaseUrl();
    expect(cooled).toBe(REMOTE);

    // Reset cache so the next resolve re-evaluates.
    resetEndpointCache();

    // Now succeed on LAN — clears the LAN cooldown and sets lanAliveUntil.
    notifyRequestSucceeded(LAN);

    // Next resolve: LAN cooldown gone + lanAliveUntil set → LAN preferred even
    // if fingerprint probe would return false (lanAlive satisfies the condition).
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("LAN success when cached decision is remote → cache dropped, LAN preferred next", async () => {
    // Force a cached decision pointing at REMOTE by simulating an off-LAN scenario.
    mockNetwork.type = "CELLULAR";
    mockNetwork.ip   = "10.0.0.1";
    const before = await resolveBaseUrl();
    expect(before).toBe(REMOTE); // cache now has `path: 'remote'`

    // Now a LAN request succeeds. notifyRequestSucceeded should drop the
    // remote-pointing cache so the next resolve flips back to LAN.
    notifyRequestSucceeded(LAN);

    // Back on Wi-Fi for the next resolve.
    mockNetwork.type = "WIFI";
    mockNetwork.ip   = "192.168.1.42";

    const after = await resolveBaseUrl();
    expect(after).toBe(LAN);
  });

  it("LAN success sets lanAliveUntil → LAN preferred even when off-LAN Wi-Fi", async () => {
    // First, build a fresh cached decision for LAN (fingerprint matches).
    const initial = await resolveBaseUrl();
    expect(initial).toBe(LAN);

    // Record LAN success — sets lanAliveUntil and drops cache if it was remote.
    // The cache currently points at LAN (path: 'lan'), so cache is NOT dropped here.
    notifyRequestSucceeded(LAN);

    // Move the phone off Wi-Fi so fingerprint won't match on the next resolve.
    mockNetwork.type = "CELLULAR";
    mockNetwork.ip   = "10.0.0.1";

    // Expire the 15s decision cache without touching lanAliveUntil — we use
    // jest fake timers to advance time past DECISION_TTL_MS (15 000 ms) so the
    // resolver re-evaluates using the actual module-level lanAliveUntil.
    jest.useFakeTimers();
    jest.advanceTimersByTime(16_000);

    // Now resolve: cache expired, fingerprint=false, but lanAlive=true → LAN.
    const url = await resolveBaseUrl();

    jest.useRealTimers();

    expect(url).toBe(LAN);
  });

  it("REMOTE success clears remoteCooldownUntil → REMOTE eligible again", async () => {
    // Cool down REMOTE.
    notifyRequestFailed(REMOTE);

    // Succeed on REMOTE.
    notifyRequestSucceeded(REMOTE);
    resetEndpointCache();

    // Move off LAN so only REMOTE is eligible.
    mockNetwork.type = "CELLULAR";
    mockNetwork.ip   = "10.0.0.1";

    // REMOTE cooldown was cleared → REMOTE should be returned.
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });

  it("notifyRequestSucceeded with unknown URL → no crash, state unchanged", async () => {
    expect(() => notifyRequestSucceeded("http://10.99.99.99:9999/api")).not.toThrow();
    // LAN still preferred since nothing changed.
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("notifyRequestSucceeded is idempotent — calling twice does not throw", async () => {
    expect(() => {
      notifyRequestSucceeded(LAN);
      notifyRequestSucceeded(LAN);
    }).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// 5. Full request-cycle sequence: primary success
// ---------------------------------------------------------------------------

describe("request-cycle sequence", () => {
  it("success on LAN → LAN returned on subsequent resolveBaseUrl call", async () => {
    // Simulate what client.ts does on a successful primary request:
    //   1. resolveBaseUrl() → LAN
    //   2. requestVia(LAN, ...) succeeds
    //   3. notifyRequestSucceeded(LAN)
    notifyRequestSucceeded(LAN);
    resetEndpointCache();
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("fail on LAN → succeed on REMOTE → REMOTE returned next", async () => {
    // client.ts fallback sequence: primary (LAN) fails, other (REMOTE) succeeds.
    notifyRequestFailed(LAN);
    notifyRequestSucceeded(REMOTE);
    resetEndpointCache();

    // Next resolve: LAN cooled, REMOTE OK.  No fingerprint match needed.
    mockNetwork.type = "CELLULAR";
    mockNetwork.ip   = "10.0.0.1";
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });

  it("LAN failed then recovered → back to LAN after notifyRequestSucceeded", async () => {
    notifyRequestFailed(LAN);       // LAN cooled
    notifyRequestSucceeded(LAN);    // LAN success later clears the cooldown
    resetEndpointCache();

    // Fingerprint matches → LAN preferred.
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("REMOTE failed + LAN success → LAN handles all subsequent traffic", async () => {
    notifyRequestFailed(REMOTE);    // remote cooled
    notifyRequestSucceeded(LAN);    // LAN handling it
    resetEndpointCache();

    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });
});

// ---------------------------------------------------------------------------
// 6. Single-URL configurations
// ---------------------------------------------------------------------------

describe("notify functions — single-URL configurations", () => {
  it("LAN-only config: failure on LAN → still returns LAN (only URL)", async () => {
    mockStoreState.baseUrl = "";      // no remote
    notifyRequestFailed(LAN);        // LAN cooled
    const url = await resolveBaseUrl();
    // decidePath last-resort: LAN is the only URL, must return it.
    expect(url).toBe(LAN);
  });

  it("LAN-only config: success on LAN → LAN returned as before", async () => {
    mockStoreState.baseUrl = "";
    notifyRequestSucceeded(LAN);
    resetEndpointCache();
    const url = await resolveBaseUrl();
    expect(url).toBe(LAN);
  });

  it("remote-only config: failure on REMOTE → still returns REMOTE (only URL)", async () => {
    mockStoreState.baseUrlLan = null;
    mockStoreState.savedNetworks = [];
    notifyRequestFailed(REMOTE);
    const url = await resolveBaseUrl();
    expect(url).toBe(REMOTE);
  });
});

describe("configuration changes", () => {
  it("does not reuse a cached base URL after Settings changes it", async () => {
    mockStoreState.baseUrlLan = null;
    expect(await resolveBaseUrl()).toBe(REMOTE);
    mockStoreState.baseUrl = "http://new-bridge.invalid/api/v1";
    expect(await resolveBaseUrl()).toBe("http://new-bridge.invalid/api/v1");
  });
  it("forgets old LAN liveness after replacing the LAN endpoint", async () => {
    await resolveBaseUrl(); notifyRequestSucceeded(LAN);
    mockStoreState.baseUrlLan = "http://new-lan.invalid/api/v1";
    mockStoreState.savedNetworks = [];
    expect(await resolveBaseUrl()).toBe(REMOTE);
  });
});
