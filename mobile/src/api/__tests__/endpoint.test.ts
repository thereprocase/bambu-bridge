/**
 * Unit tests for the pure path-decision logic in src/api/endpoint.ts.
 *
 * These tests import ONLY the pure helpers (`decidePath`, `otherUrlFor`,
 * `sameOrigin`, `prefixOf`) — no native expo-network, no zustand stores.
 * That keeps the suite runnable in a Node.js jest environment without any
 * Expo mocks.
 *
 * Truth table covered by the decidePath suite:
 *
 *  #  fpMatch  lanAlive  lanCooled  remoteCooled  lanUrl  remUrl  → path
 *  1  false    false     false      false         set     set     remote   (no signal for LAN)
 *  2  true     false     false      false         set     set     lan      (fingerprint wins)
 *  3  false    true      false      false         set     set     lan      (lanAlive wins — the incident scenario)
 *  4  true     true      false      false         set     set     lan      (both signals, LAN preferred)
 *  5  true     false     true       false         set     set     remote   (LAN cooled, go remote)
 *  6  false    false     false      true          set     set     remote   (remote cooled but no LAN signal → still remote)
 *  7  true     false     false      true          set     set     lan      (remote cooled + fingerprint → LAN)
 *  8  false    true      false      true          set     set     lan      (remote cooled + lanAlive → LAN)
 *  9  true     false     true       true          set     set     lan      (BOTH cooled → ignore cooldowns, fingerprint → LAN)
 * 10  false    false     true       true          set     set     remote   (BOTH cooled → ignore cooldowns, no signal → remote)
 * 11  true     false     false      false         set     null    lan      (no remUrl → LAN only)
 * 12  false    false     false      false         null    set     remote   (no LAN url → remote only)
 * 13  false    false     false      false         null    null    null     (nothing configured)
 * 14  true     false     false      false         null    set     remote   (fingerprint but no LAN url → remote)
 * 15  true     false     true       false         set     null    lan      (LAN cooled but it's the only URL → use it)
 */

import { decidePath, otherUrlFor, prefixOf, sameOrigin } from "../endpoint";

// ---------------------------------------------------------------------------
// Module-level store mock — decidePath takes its inputs as parameters so it
// doesn't touch the store. otherUrlFor does read the store; we mock it here.
// ---------------------------------------------------------------------------

const mockStoreState: { baseUrl: string; baseUrlLan: string | null } = {
  baseUrl: "",
  baseUrlLan: null,
};

jest.mock("../../store/bridge", () => ({
  useBridgeStore: {
    getState: () => mockStoreState,
  },
}));

// ---------------------------------------------------------------------------
// decidePath truth table
// ---------------------------------------------------------------------------

const LAN = "http://192.168.1.10:8080/api/v1";
const REMOTE = "http://100.64.1.2:8080/api/v1";

describe("decidePath", () => {
  // Helper to call with sensible defaults
  function decide(overrides: Partial<Parameters<typeof decidePath>[0]>) {
    return decidePath({
      baseUrl: REMOTE,
      baseUrlLan: LAN,
      fingerprintMatch: false,
      lanAlive: false,
      lanCooled: false,
      remoteCooled: false,
      ...overrides,
    });
  }

  it("#1 no LAN signal → remote", () => {
    const r = decide({ fingerprintMatch: false, lanAlive: false });
    expect(r?.path).toBe("remote");
    expect(r?.url).toBe(REMOTE);
  });

  it("#2 fingerprint match → LAN", () => {
    const r = decide({ fingerprintMatch: true });
    expect(r?.path).toBe("lan");
    expect(r?.url).toBe(LAN);
  });

  it("#3 lanAlive (no fingerprint) → LAN — the incident scenario", () => {
    // Android demoted Wi-Fi; fingerprint probe returns false.
    // But LAN recently succeeded, so we should still prefer it.
    const r = decide({ fingerprintMatch: false, lanAlive: true });
    expect(r?.path).toBe("lan");
    expect(r?.url).toBe(LAN);
  });

  it("#4 both signals → LAN", () => {
    const r = decide({ fingerprintMatch: true, lanAlive: true });
    expect(r?.path).toBe("lan");
    expect(r?.url).toBe(LAN);
  });

  it("#5 LAN cooled + fingerprint → remote", () => {
    const r = decide({ fingerprintMatch: true, lanCooled: true });
    expect(r?.path).toBe("remote");
  });

  it("#6 remote cooled + no LAN signal → still remote (only option)", () => {
    const r = decide({ remoteCooled: true, fingerprintMatch: false, lanAlive: false });
    // LAN not eligible (no signal), remote cooled but only option.
    // Expected: remote (cooled but fall-through).
    expect(r?.path).toBe("remote");
  });

  it("#7 remote cooled + fingerprint match → LAN", () => {
    const r = decide({ remoteCooled: true, fingerprintMatch: true });
    expect(r?.path).toBe("lan");
  });

  it("#8 remote cooled + lanAlive → LAN", () => {
    const r = decide({ remoteCooled: true, lanAlive: true });
    expect(r?.path).toBe("lan");
  });

  it("#9 BOTH cooled + fingerprint → LAN (cooldowns ignored)", () => {
    // Both cooled — both-cooled guard kicks in; fingerprint match → LAN.
    const r = decide({ lanCooled: true, remoteCooled: true, fingerprintMatch: true });
    expect(r?.path).toBe("lan");
  });

  it("#10 BOTH cooled + no signal → remote (cooldowns ignored, fall to remote)", () => {
    const r = decide({ lanCooled: true, remoteCooled: true, fingerprintMatch: false, lanAlive: false });
    expect(r?.path).toBe("remote");
  });

  it("#11 no remote URL → use LAN regardless of signal", () => {
    const r = decide({ baseUrl: null, fingerprintMatch: true });
    expect(r?.path).toBe("lan");
    expect(r?.url).toBe(LAN);
  });

  it("#12 no LAN URL → remote always", () => {
    const r = decide({ baseUrlLan: null, fingerprintMatch: true });
    expect(r?.path).toBe("remote");
    expect(r?.url).toBe(REMOTE);
  });

  it("#13 neither URL → null", () => {
    const r = decide({ baseUrl: null, baseUrlLan: null });
    expect(r).toBeNull();
  });

  it("#14 fingerprint match but no LAN URL → remote", () => {
    const r = decide({ baseUrlLan: null, fingerprintMatch: true });
    expect(r?.path).toBe("remote");
  });

  it("#15 LAN cooled but only URL → still returns LAN (last resort)", () => {
    const r = decide({ baseUrl: null, baseUrlLan: LAN, lanCooled: true, fingerprintMatch: true });
    // remoteCooled is false but remote doesn't exist. LAN is all we have.
    expect(r?.url).toBe(LAN);
    expect(r?.path).toBe("lan");
  });

  it("prefers LAN when LAN is not cooled and remote IS cooled even without signal (both unavailable edge)", () => {
    // Edge: remote cooled + LAN has no signal but IS the only URL.
    const r = decide({ baseUrl: null, baseUrlLan: LAN, remoteCooled: true, fingerprintMatch: false, lanAlive: false });
    expect(r?.url).toBe(LAN);
  });

  it("URL with trailing slash matches same origin", () => {
    const r = decidePath({
      baseUrl: "http://100.64.1.2:8080/api/v1/",
      baseUrlLan: "http://192.168.1.10:8080/api/v1/",
      fingerprintMatch: true,
      lanAlive: false,
      lanCooled: false,
      remoteCooled: false,
    });
    expect(r?.path).toBe("lan");
  });
});

// ---------------------------------------------------------------------------
// otherUrlFor — origin matching, trailing slashes, same-origin edge cases
// ---------------------------------------------------------------------------

describe("otherUrlFor", () => {
  beforeEach(() => {
    mockStoreState.baseUrl = REMOTE;
    mockStoreState.baseUrlLan = LAN;
  });

  it("LAN URL → returns remote", () => {
    expect(otherUrlFor(LAN)).toBe(REMOTE);
  });

  it("remote URL → returns LAN", () => {
    expect(otherUrlFor(REMOTE)).toBe(LAN);
  });

  it("LAN URL with trailing slash → still returns remote", () => {
    expect(otherUrlFor(LAN + "/")).toBe(REMOTE);
  });

  it("remote URL with trailing slash → still returns LAN", () => {
    expect(otherUrlFor(REMOTE + "/")).toBe(LAN);
  });

  it("LAN URL with sub-path → returns remote (same origin)", () => {
    expect(otherUrlFor("http://192.168.1.10:8080/api/v1/printers")).toBe(REMOTE);
  });

  it("no LAN configured → returns null for remote", () => {
    mockStoreState.baseUrlLan = null;
    expect(otherUrlFor(REMOTE)).toBeNull();
  });

  it("no remote configured → returns null for LAN", () => {
    mockStoreState.baseUrl = "";
    expect(otherUrlFor(LAN)).toBeNull();
  });

  it("unknown URL → returns null", () => {
    expect(otherUrlFor("http://10.0.0.1:9999/api/v1")).toBeNull();
  });

  it("same origin for both (misconfigured) → returns null", () => {
    mockStoreState.baseUrl = LAN;
    mockStoreState.baseUrlLan = LAN;
    expect(otherUrlFor(LAN)).toBeNull();
  });

  it("malformed URL → returns null gracefully", () => {
    expect(otherUrlFor("not-a-url")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// sameOrigin — exported for test visibility
// ---------------------------------------------------------------------------

describe("sameOrigin", () => {
  it("same scheme+host+port → true", () => {
    expect(sameOrigin("http://host:8080/a", "http://host:8080/b")).toBe(true);
  });

  it("trailing slash → true", () => {
    expect(sameOrigin("http://host:8080/", "http://host:8080/api")).toBe(true);
  });

  it("different port → false", () => {
    expect(sameOrigin("http://host:8080/a", "http://host:9090/a")).toBe(false);
  });

  it("different host → false", () => {
    expect(sameOrigin("http://host1:8080/a", "http://host2:8080/a")).toBe(false);
  });

  it("http vs https → false", () => {
    expect(sameOrigin("http://host:8080/a", "https://host:8080/a")).toBe(false);
  });

  it("malformed URL → false", () => {
    expect(sameOrigin("not-a-url", "http://host:8080/")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// prefixOf — boundary conditions
// ---------------------------------------------------------------------------

describe("prefixOf", () => {
  it("typical /24 → first three octets", () => {
    expect(prefixOf("192.168.1.109")).toBe("192.168.1");
  });

  it("edge address 0.0.0.0 → 0.0.0", () => {
    expect(prefixOf("0.0.0.0")).toBe("0.0.0");
  });

  it("max address 255.255.255.255 → 255.255.255", () => {
    expect(prefixOf("255.255.255.255")).toBe("255.255.255");
  });

  it("octet > 255 → null", () => {
    expect(prefixOf("192.168.999.1")).toBeNull();
  });

  it("IPv6 → null", () => {
    expect(prefixOf("::1")).toBeNull();
  });

  it("null input → null", () => {
    expect(prefixOf(null)).toBeNull();
  });

  it("undefined input → null", () => {
    expect(prefixOf(undefined)).toBeNull();
  });

  it("empty string → null", () => {
    expect(prefixOf("")).toBeNull();
  });

  it("partial IP (three octets) → null", () => {
    expect(prefixOf("192.168.1")).toBeNull();
  });

  it("leading whitespace stripped", () => {
    expect(prefixOf("  10.0.0.1")).toBe("10.0.0");
  });
});
