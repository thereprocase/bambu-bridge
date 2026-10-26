/**
 * SpoolRecord — the Spoolman record as a card: swatch and hex, vendor,
 * product, material, SKU, lot, weights, dates, location, price, comment.
 * Only the fields the bridge sent are drawn; an older bridge shows fewer
 * rows, never blanks.
 */

import { Text, View } from "react-native";

import type { InventorySpool } from "../api/inventory";
import { dateText, fillText } from "../lib/inventoryView";
import { spoolColor } from "../lib/shelf";
import { useTheme } from "../theme/ThemeProvider";
import { Surface } from "./Surface";
import { Swatch } from "./Swatch";

/** Label · value pairs, in the order the card shows them; absent fields are left out. */
export function recordRows(spool: InventorySpool, now = new Date()): [string, string][] {
  const f = spool.filament;
  const rows: [string, string | null | undefined][] = [
    ["Vendor", f.vendor?.name],
    ["Product", f.name],
    ["Material", f.material?.toUpperCase()],
    ["SKU", f.article_number],
    ["Lot", spool.lot_nr],
    ["Filament", fillText(spool)],
    ["Used", spool.used_weight != null ? `${Math.round(spool.used_weight).toLocaleString("en-US")} g` : null],
    ["Empty spool", (spool.spool_weight ?? f.spool_weight) != null ? `${Math.round((spool.spool_weight ?? f.spool_weight)!)} g` : null],
    ["First used", dateText(spool.first_used, now)],
    ["Last used", dateText(spool.last_used, now)],
    ["Location", spool.location],
    ["Price", spool.price != null ? spool.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : f.price != null ? f.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : null],
    ["Diameter", f.diameter != null ? `${f.diameter} mm` : null],
    ["Density", f.density != null ? `${f.density} g/cm³` : null],
  ];
  return rows.filter((r): r is [string, string] => typeof r[1] === "string" && r[1].trim() !== "");
}

export function SpoolRecord({ spool }: { spool: InventorySpool }) {
  const { c, space, type } = useTheme();
  const colour = spoolColor(spool);
  const rows = recordRows(spool);
  return (
    <Surface padded style={{ gap: space.md }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: space.md }}>
        <Swatch color={colour} size={48} dimmed={spool.archived} />
        <View style={{ flex: 1, gap: 2 }}>
          <Text style={[type.h2, { color: c.text }]}>Record</Text>
          <Text style={[type.mono, { color: c.muted }]}>{colour ?? "no colour"}{spool.archived ? " · archived" : ""}</Text>
        </View>
      </View>
      <View style={{ gap: space.xs }}>
        {rows.map(([label, value]) => (
          <View key={label} style={{ flexDirection: "row", gap: space.md, alignItems: "flex-start" }}>
            <Text style={[type.caption, { color: c.muted, textTransform: "uppercase", width: 88, paddingTop: 2 }]}>{label}</Text>
            <Text style={[type.body, { color: c.text, flex: 1 }]}>{value}</Text>
          </View>
        ))}
      </View>
      {!!spool.comment?.trim() && (
        <View style={{ gap: 2, borderTopWidth: 1, borderTopColor: c.borderSoft, paddingTop: space.sm }}>
          <Text style={[type.caption, { color: c.muted, textTransform: "uppercase" }]}>Comment</Text>
          <Text style={[type.small, { color: c.text }]}>{spool.comment.trim()}</Text>
        </View>
      )}
    </Surface>
  );
}
