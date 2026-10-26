/**
 * The Menu behind the header's gear: the screens that left the bottom bar,
 * the bridge queue, and Settings. One list, so the layout (which tabs are
 * folded) and the menu screen (what it offers) can't drift apart.
 */

import type { ComponentProps } from "react";
import type { Ionicons } from "@expo/vector-icons";

export type IoniconName = ComponentProps<typeof Ionicons>["name"];

export interface MenuItem {
  id: string;
  label: string;
  /** One line under the label: what the owner finds there. */
  hint: string;
  icon: IoniconName;
  href: string;
  /** A tab that is hidden from the bar; opened with dismissTo so the menu closes over it. */
  tab: boolean;
}

/** Tabs that stay in the bottom bar, in order. Everything else in (tabs) is folded into the menu. */
export const BAR_TABS = ["status", "print", "filament", "inventory"] as const;

export const MENU_ITEMS: readonly MenuItem[] = [
  { id: "controls", label: "Controls", hint: "Lights, speed, temperatures, fans, camera, movement", icon: "options-outline", href: "/(tabs)/controls", tab: true },
  { id: "activity", label: "Activity", hint: "Events and diagnostics since the bridge started", icon: "time-outline", href: "/(tabs)/activity", tab: true },
  { id: "advanced", label: "Advanced", hint: "XCam, AMS settings, calibration, nozzle, G-code", icon: "flask-outline", href: "/(tabs)/advanced", tab: true },
  { id: "queue", label: "Bridge queue and recovery", hint: "Native upload receipts, backups, restore", icon: "list-outline", href: "/native-recovery", tab: false },
  { id: "settings", label: "Settings", hint: "Bridge address, pairing, printers, Spoolman", icon: "settings-outline", href: "/settings", tab: false },
];

export const FOLDED_TABS = MENU_ITEMS.filter((m) => m.tab).map((m) => m.id);
