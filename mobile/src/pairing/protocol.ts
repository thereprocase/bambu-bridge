/** QR data is untrusted until validated and its key verified by native TLS. */
export interface Invitation {
  type: "bambu-bridge-pair";
  version: 1;
  base_url: string;
  spki: string;
  secret: string;
  expires: number;
}
export interface PairedProfile {
  version: 1;
  baseUrl: string;
  spki: string;
  token: string;
  deviceId: string;
  name: string;
  remoteUrl?: string;
}

export function secureBase(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password ||
      url.search || url.hash || url.pathname.replace(/\/+$/, "") !== "/api/v1") {
    throw new Error("Use an HTTPS bridge address ending in /api/v1.");
  }
  return url.toString().replace(/\/+$/, "");
}

function validPin(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9+/]{43}=$/.test(value);
}

export function parseInvitation(text: string, now = Date.now()): Invitation {
  if (text.length > 4096) throw new Error("This is not a Bambu Bridge pairing code.");
  let p: Partial<Invitation>;
  try { p = JSON.parse(text); } catch { throw new Error("This is not a Bambu Bridge pairing code."); }
  if (!p || p.type !== "bambu-bridge-pair" || p.version !== 1 ||
      typeof p.base_url !== "string" || !validPin(p.spki) ||
      typeof p.secret !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(p.secret) ||
      !Number.isSafeInteger(p.expires)) throw new Error("Invalid Bambu Bridge pairing code.");
  if (p.expires! * 1000 <= now || p.expires! * 1000 > now + 15 * 60_000) {
    throw new Error("This code has expired or the phone clock is incorrect. Create a new code.");
  }
  return { ...p, base_url: secureBase(p.base_url) } as Invitation;
}

export function parseProfile(text: string): PairedProfile {
  const p = JSON.parse(text) as PairedProfile;
  if (!p || p.version !== 1 || !validPin(p.spki) || typeof p.token !== "string" ||
      !/^bbd_[A-Za-z0-9_-]{43}$/.test(p.token) || typeof p.deviceId !== "string" ||
      !/^[a-f0-9]{24}$/.test(p.deviceId) || typeof p.name !== "string") {
    throw new Error("Couldn't read the paired bridge. Pair again.");
  }
  return { ...p, baseUrl: secureBase(p.baseUrl),
    remoteUrl: p.remoteUrl ? secureBase(p.remoteUrl) : undefined };
}
