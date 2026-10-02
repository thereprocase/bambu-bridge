// ams-mapping.js — Orca's "Send to printer" filament mapping, in the browser.
//
// Pure functions, no DOM, so node --test can load them. The wire format is
// exactly what Orca puts in project_file (SelectMachineDialog::
// get_ams_mapping_result): one entry per PROJECT filament, in project order,
// each the global AMS tray (ams_id * 4 + slot; 0 = physical slot 1) or -1 for
// a filament the plate does not use or that rides the external spool. The
// bridge forwards it to the printer unchanged.

/**
 * Number of filaments in the project: Orca sizes ams_mapping by
 * preset_bundle->filament_presets, which the 3MF records as the per-filament
 * arrays in Metadata/project_settings.config. Without that file, fall back to
 * the highest filament id the plate uses.
 * @param {object|null} settings parsed project_settings.config
 * @param {Array<{index:number}>} filaments plate filaments (1-based ids)
 */
export function projectFilamentCount(settings, filaments) {
  const used = Math.max(0, ...filaments.map((f) => f.index));
  const list = settings && (settings.filament_settings_id || settings.filament_type);
  return Array.isArray(list) && list.length >= used ? list.length : used;
}

/**
 * Default tray per filament, as DevMappingUtil::ams_filament_mapping does it:
 * only loaded trays of the same material type are candidates, the closest
 * colour (CIE76 ΔE in Lab) wins, a tray already taken is used only when no
 * free one matches, and a filament with no match stays unassigned (null) so
 * the user must pick, as Orca blocks Send until every filament is mapped.
 * @param {Array<{index:number,type:string|null,color:string|null}>} filaments
 * @param {Array<{physical_slot:number,type:string|null,color:string|null,state?:string}>} slots
 * @returns {Map<number, number|null>} filament id → tray (0-based) or null
 */
export function autoMatch(filaments, slots) {
  const trays = slots.filter((s) => s && s.type && s.state !== 'empty')
    .map((s) => ({ tray: s.physical_slot - 1, type: s.type, color: s.color }));
  const result = new Map(filaments.map((f) => [f.index, null]));
  const taken = new Set();
  const open = new Set(filaments.map((f) => f.index));
  for (let round = 0; round < filaments.length; round++) {
    let best = null;
    for (const f of filaments) {
      if (!open.has(f.index)) continue;
      const same = trays.filter((t) => t.type === f.type);
      const free = same.filter((t) => !taken.has(t.tray));
      for (const t of (free.length ? free : same)) {
        const d = colorDistance(f.color, t.color);
        if (!best || d < best.d) best = { f: f.index, tray: t.tray, d };
      }
    }
    if (!best) break;
    result.set(best.f, best.tray);
    taken.add(best.tray);
    open.delete(best.f);
  }
  return result;
}

/**
 * The project_file ams_mapping: -1 everywhere, then each plate filament's tray.
 * @param {Array<{index:number, tray:number|'external'|null}>} filaments
 * @param {number} count project filament count
 */
export function amsMapping(filaments, count) {
  const out = new Array(count).fill(-1);
  for (const f of filaments) {
    if (Number.isInteger(f.tray) && f.index >= 1 && f.index <= count) out[f.index - 1] = f.tray;
  }
  return out;
}

// CIE76 ΔE between two #RRGGBB colours (Orca GuiColor calc_color_distance).
// A missing colour matches nothing in particular: a large constant distance.
export function colorDistance(a, b) {
  const la = toLab(a);
  const lb = toLab(b);
  if (!la || !lb) return 1000;
  return Math.hypot(la[0] - lb[0], la[1] - lb[1], la[2] - lb[2]);
}

function toLab(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(hex || '');
  if (!m) return null;
  // sRGB → linear → XYZ (D65) → Lab
  const [r, g, b] = m.slice(1, 4).map((h) => {
    const c = parseInt(h, 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047;
  const y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.0;
  const z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883;
  const f = (t) => (t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 * t + 16) / 116);
  const [fx, fy, fz] = [f(x), f(y), f(z)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}
