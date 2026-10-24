/**
 * Filament used by the running print, for the Status tab's progress card.
 * Polls the bridge every 10 s (its own delivery tick) while mounted and the
 * bridge reports a print; renders nothing when inventory is off or idle.
 */

import { useEffect, useRef, useState } from "react";
import { Text, View } from "react-native";

import {
  inventoryLiveUsage,
  inventorySpools,
  inventoryStatus,
  type InventorySpool,
  type LiveUsage as LiveUsageReport,
} from "../api/inventory";
import { usageLines } from "../lib/liveUsage";
import { useTheme } from "../theme/ThemeProvider";

const POLL_MS = 10_000;

export function LiveUsage({ printer }: { printer: string }) {
  const { c, space, type } = useTheme();
  const [report, setReport] = useState<LiveUsageReport | null>(null);
  const [spools, setSpools] = useState<InventorySpool[]>([]);
  const spoolsFor = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      try {
        const status = await inventoryStatus();
        if (!active) return;
        if (!(status.configured && status.api_version === 2)) {
          setReport(null);
          return;
        }
        const next = await inventoryLiveUsage(printer);
        if (!active) return;
        setReport(next);
        // Spool names are looked up once per print, when a slot first needs one.
        if (next.printing && next.attempt_id && spoolsFor.current !== next.attempt_id) {
          spoolsFor.current = next.attempt_id;
          const records = await inventorySpools().catch(() => []);
          if (active) setSpools(records);
        }
      } catch {
        if (active) setReport(null); // the line simply stays away until the bridge answers
      } finally {
        if (active) timer = setTimeout(() => void tick(), POLL_MS);
      }
    };
    void tick();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [printer]);

  const lines = report ? usageLines(report, spools) : null;
  if (!lines) return null;
  return (
    <View style={{ gap: 2 }}>
      <Text style={[type.body, { color: c.text }]}>{lines.headline}</Text>
      {lines.slots.map((line) => (
        <Text key={line.text} style={[type.small, { color: line.unassigned ? c.warn : c.muted }]}>
          {line.text}
        </Text>
      ))}
      {lines.sent && <Text style={[type.caption, { color: c.muted }]}>{lines.sent}</Text>}
    </View>
  );
}
