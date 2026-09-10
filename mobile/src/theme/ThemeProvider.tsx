/**
 * Theme context — palette + spacing + typography in one read.
 *
 * Mode follows the system by default (Appearance.getColorScheme()), and
 * persists an override the user picks in Settings via MMKV. The override
 * wins over the system pick so the user can lock the app to a mode even
 * when their OS is auto.
 */

import { Appearance } from "react-native";
import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { kv } from "../lib/kv";
import { Mode, Palette, palettes, radius, space, typography } from "./tokens";

const KV_MODE = "ui.themeOverride";  // "dark" | "light" | "" (auto)

export interface Theme {
  mode: Mode;
  c: Palette;
  type: typeof typography;
  space: typeof space;
  radius: typeof radius;
  setOverride(value: Mode | null): void;
  override: Mode | null;
}

const ThemeContext = createContext<Theme | null>(null);

function resolveSystem(): Mode {
  return Appearance.getColorScheme() === "light" ? "light" : "dark";
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const initialOverride = (kv.getString(KV_MODE) || null) as Mode | null;
  const [override, setOverrideState] = useState<Mode | null>(initialOverride);
  const [systemMode, setSystemMode] = useState<Mode>(resolveSystem());

  useEffect(() => {
    const sub = Appearance.addChangeListener(() => setSystemMode(resolveSystem()));
    return () => sub.remove();
  }, []);

  const value = useMemo<Theme>(() => {
    const mode: Mode = override ?? systemMode;
    return {
      mode,
      c: palettes[mode],
      type: typography,
      space,
      radius,
      override,
      setOverride: (v) => {
        setOverrideState(v);
        if (v) kv.set(KV_MODE, v);
        else kv.remove(KV_MODE);
      },
    };
  }, [override, systemMode]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): Theme {
  const v = useContext(ThemeContext);
  if (!v) throw new Error("useTheme must be used inside <ThemeProvider>");
  return v;
}
