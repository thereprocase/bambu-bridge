/** Gridline adapted from thereprocase.github.io/public/gridline.
 * Light uses the source palette; dark preserves hierarchy and semantics.
 * Fonts are bundled for offline operation.
 */
export type Mode = "dark" | "light";
export interface Palette {
  bg: string; surface: string; surface2: string; surface3: string;
  border: string; borderSoft: string; text: string; muted: string;
  accent: string; accentDim: string; accentDeep: string;
  onAccent: string; onDanger: string; title: string; onTitle: string;
  warn: string; danger: string; dangerDeep: string; blue: string;
}
export const light: Palette = {
  bg: "#C6C6C6", surface: "#FFFFFF", surface2: "#E8E8E8", surface3: "#F2F2F2",
  border: "#666666", borderSoft: "#9A9A9A", text: "#101010", muted: "#3D3D3D",
  accent: "#0000A8", accentDim: "#000078", accentDeep: "#E8E8F8",
  onAccent: "#FFFFFF", onDanger: "#FFFFFF", title: "#0000A8", onTitle: "#FFFFFF",
  warn: "#8A5900", danger: "#B3261E", dangerDeep: "#FBEAE9", blue: "#0000A8",
};
export const dark: Palette = {
  bg: "#181818", surface: "#242424", surface2: "#303030", surface3: "#383838",
  border: "#9A9A9A", borderSoft: "#666666", text: "#FFFFFF", muted: "#C6C6C6",
  accent: "#A6B8FF", accentDim: "#829AFF", accentDeep: "#24244A",
  onAccent: "#101010", onDanger: "#101010", title: "#0000A8", onTitle: "#FFFFFF",
  warn: "#F0BD55", danger: "#FF8A82", dangerDeep: "#4A2424", blue: "#A6B8FF",
};
export const palettes: Record<Mode, Palette> = { dark, light };
export const typography = {
  display: { fontSize: 32, fontFamily: "IBMPlexSans_700Bold" },
  h1: { fontSize: 22, fontFamily: "IBMPlexSans_600SemiBold" },
  h2: { fontSize: 17, fontFamily: "IBMPlexSans_600SemiBold" },
  body: { fontSize: 15, fontFamily: "IBMPlexSans_400Regular" },
  small: { fontSize: 13, fontFamily: "IBMPlexSans_400Regular" },
  caption: { fontSize: 11, fontFamily: "IBMPlexMono_500Medium" },
  mono: { fontSize: 14, fontFamily: "IBMPlexMono_500Medium" },
};
export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 };
export const radius = { sm: 0, md: 0, lg: 0, xl: 0, pill: 0 };
