/**
 * The last few prints and what happened to their filament: grams charged
 * per spool, a refill split by layers, anything left uncharged, anything
 * still waiting for the owner. Each charged spool offers "Wrong spool?".
 */

import { Text, View } from "react-native";

import type { AccountingRow, Charge } from "../api/inventory";
import { outcomeWord, recentLine, shortfallLine, splitLine, whenText } from "../lib/heldPrints";
import { useTheme } from "../theme/ThemeProvider";
import { Button } from "./Button";
import { Surface } from "./Surface";

export function RecentPrints({ rows, onWrongSpool }: { rows: AccountingRow[]; onWrongSpool?: (row: AccountingRow, charge: Charge) => void }) {
  const { c, space, type } = useTheme();
  if (!rows.length) return null;
  return (
    <Surface padded style={{ gap: space.md }}>
      <Text style={[type.h2, { color: c.text }]}>Recent prints</Text>
      {rows.map((row, index) => {
        const split = splitLine(row);
        return (
          <View
            key={row.attempt_id}
            style={{ gap: 2, borderTopWidth: index ? 1 : 0, borderTopColor: c.borderSoft, paddingTop: index ? space.md : 0 }}
          >
            <Text style={[type.body, { color: c.text }]} numberOfLines={1}>{row.title || "Print"}</Text>
            <Text style={[type.small, { color: c.muted }]}>
              {[outcomeWord(row), whenText(row)].filter(Boolean).join(" ")} · {recentLine(row)}
            </Text>
            {split && <Text style={[type.small, { color: c.muted }]}>{split}</Text>}
            {row.delivery_state !== "review" &&
              !(row.charged ?? []).length &&
              !row.resolution &&
              (row.shortfalls ?? []).map((s, i) => (
                <Text key={i} style={[type.small, { color: c.muted }]}>{shortfallLine(s)}</Text>
              ))}
            {onWrongSpool && (row.charged ?? []).filter((ch) => ch.grams > 0).length > 0 && (
              <View style={{ flexDirection: "row", gap: space.sm, flexWrap: "wrap", marginTop: space.xs }}>
                {(row.charged ?? []).filter((ch) => ch.grams > 0).map((ch) => (
                  <Button
                    key={ch.spool_id}
                    label={(row.charged ?? []).length > 1 ? `Wrong spool? (#${ch.spool_id})` : "Wrong spool?"}
                    variant="secondary"
                    onPress={() => onWrongSpool(row, ch)}
                  />
                ))}
              </View>
            )}
          </View>
        );
      })}
    </Surface>
  );
}
