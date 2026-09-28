import { light, dark, radius, typography } from "../../theme/tokens";

function luminance(hex: string) {
  const rgb = hex.slice(1).match(/../g)!.map(value => parseInt(value, 16) / 255)
    .map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
}
function contrast(a: string, b: string) {
  const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (values[0] + 0.05) / (values[1] + 0.05);
}
test.each([["light", light], ["dark", dark]] as const)("%s text and action labels meet AA contrast", (_, c) => {
  for (const surface of [c.bg, c.surface, c.surface2, c.surface3]) {
    expect(contrast(c.text, surface)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(c.muted, surface)).toBeGreaterThanOrEqual(4.5);
  }
  expect(contrast(c.onAccent, c.accent)).toBeGreaterThanOrEqual(4.5);
  expect(contrast(c.onDanger, c.danger)).toBeGreaterThanOrEqual(4.5);
  expect(contrast(c.onTitle, c.title)).toBeGreaterThanOrEqual(4.5);
});
test("square panes and bundled Plex families are the shared default", () => {
  expect(Object.values(radius).every(value => value === 0)).toBe(true);
  expect(Object.values(typography).every(value => value.fontFamily.startsWith("IBMPlex"))).toBe(true);
});
