import type { PrinterSnapshot } from "../api/types";
import type { LiveStatus } from "../ws/live";
import { printerStateLabels } from "./printerState";
import type { Reach } from "../store/net";

export function connectionStripLabels(snapshot: PrinterSnapshot | null, status: LiveStatus, hasPrinter: boolean, reach: Reach = "unknown") {
  const bridge = status === "open" || reach === "lan" || reach === "remote" ? "Connected"
    : reach === "down" ? "Unreachable" : status === "connecting" ? "Connecting" : hasPrinter ? "Checking" : "Unknown";
  const facts = printerStateLabels(snapshot, status);
  const activity = facts.live ? facts.activity : "Unknown";
  const symbol = activity === "Printing" ? "▶" : activity === "Paused" ? "Ⅱ"
    : activity === "Idle" || activity === "Finished" ? "■"
      : activity === "Print failed" ? "!" : activity === "Preparing" ? "◷" : "?";
  return { bridge, activity, symbol, connection: facts.connection };
}
