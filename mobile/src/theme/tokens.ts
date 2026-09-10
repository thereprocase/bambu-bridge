/**
 * Design tokens for the original Bambu Bridge printer interface.
 *
 * The mock uses oklch() for the accent ramps; React Native styles don't
 * accept oklch yet, so each value is pre-converted to its sRGB hex
 * equivalent here. If a future RN version supports oklch we can drop the
 * conversion — until then, keep both forms in the comment so anyone can
 * cross-check what they're looking at against the design source of truth.
 */

export type Mode = "dark" | "light";

export interface Palette {
  bg: string;
  surface: string;
  surface2: string;
  surface3: string;
  border: string;
  borderSoft: string;
  text: string;
  muted: string;
  accent: string;       // primary positive — the bright green dot
  accentDim: string;    // ringed-button background
  accentDeep: string;   // ringed-button border
  warn: string;         // amber — door open, low spool
  danger: string;       // thermal / failed
  dangerDeep: string;
  blue: string;         // informational accent
}

// Dark mode — index.html :root
export const dark: Palette = {
  bg: "#0a0b0d",
  surface: "#14161a",
  surface2: "#1c1f24",
  surface3: "#23272e",
  border: "#2a2e36",
  borderSoft: "#1f2329",
  text: "#e6e8eb",
  muted: "#8a9099",
  accent: "#a3c75a",       // oklch(0.82 0.17 145)
  accentDim: "#5a7d2a",    // oklch(0.52 0.12 145)
  accentDeep: "#1d3010",   // oklch(0.28 0.07 145)
  warn: "#d4b04a",         // oklch(0.78 0.16 80)
  danger: "#d05540",       // oklch(0.65 0.22 25)
  dangerDeep: "#5a1d11",   // oklch(0.32 0.13 25)
  blue: "#6cb0e0",         // oklch(0.78 0.13 235)
};

// Light mode — index.html [data-theme=light]
export const light: Palette = {
  bg: "#ececef",
  surface: "#ffffff",
  surface2: "#f4f5f7",
  surface3: "#e8eaee",
  border: "#d2d5db",
  borderSoft: "#e3e5ea",
  text: "#0a0b0d",
  muted: "#5a6068",
  accent: "#a3c75a",
  accentDim: "#7a9c40",
  accentDeep: "#cce4a8",   // oklch(0.88 0.11 145) — much lighter for light bg
  warn: "#c79a32",
  danger: "#b54838",
  dangerDeep: "#f2c8c0",
  blue: "#3d7eb0",
};

export const palettes: Record<Mode, Palette> = { dark, light };

// Type ramps — Inter Tight for UI, JetBrains Mono for numbers/captions.
// Sized to read on a 5.5"–6.7" phone at arm's length.
export const typography = {
  display: { fontSize: 32, fontWeight: "700" as const, fontFamily: "Inter Tight" },
  h1:      { fontSize: 22, fontWeight: "600" as const, fontFamily: "Inter Tight" },
  h2:      { fontSize: 17, fontWeight: "600" as const, fontFamily: "Inter Tight" },
  body:    { fontSize: 15, fontWeight: "400" as const, fontFamily: "Inter Tight" },
  small:   { fontSize: 13, fontWeight: "400" as const, fontFamily: "Inter Tight" },
  caption: { fontSize: 11, fontWeight: "500" as const, fontFamily: "JetBrains Mono" },
  mono:    { fontSize: 14, fontWeight: "500" as const, fontFamily: "JetBrains Mono" },
};

// Spacing — 4px base grid; reach for these instead of magic numbers.
export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 };

// Radii — soft cards at 12, pills at 999.
export const radius = { sm: 6, md: 10, lg: 14, xl: 20, pill: 999 };
