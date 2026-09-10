/**
 * Tiny toast — no third-party lib. The store holds at most one message
 * at a time; auto-clears after `ttlMs`. Used for transient confirmations
 * ("Light off", "Temperature set"), errors from quick-tap actions, and
 * the "undo" prompt for preset taps (design review).
 */

import { useEffect } from "react";
import { Animated, Pressable, Text } from "react-native";
import { create } from "zustand";

import { useTheme } from "../theme/ThemeProvider";

export type ToastSeverity = "info" | "success" | "warn" | "danger";

interface ToastMessage {
  id: number;
  text: string;
  severity: ToastSeverity;
  /** Optional action button (e.g. "Undo"). */
  action?: { label: string; onPress(): void };
  ttlMs: number;
}

interface ToastStore {
  current: ToastMessage | null;
  show(text: string, opts?: Partial<Omit<ToastMessage, "id" | "text">>): void;
  dismiss(): void;
}

let counter = 0;

export const useToastStore = create<ToastStore>((set) => ({
  current: null,
  show: (text, opts) => {
    counter += 1;
    set({
      current: {
        id: counter,
        text,
        severity: opts?.severity ?? "info",
        action: opts?.action,
        ttlMs: opts?.ttlMs ?? 4000,
      },
    });
  },
  dismiss: () => set({ current: null }),
}));

export function ToastHost() {
  const { c, radius, space, type } = useTheme();
  const current = useToastStore((s) => s.current);
  const dismiss = useToastStore((s) => s.dismiss);

  useEffect(() => {
    if (!current) return;
    const t = setTimeout(dismiss, current.ttlMs);
    return () => clearTimeout(t);
  }, [current, dismiss]);

  if (!current) return null;

  const tint =
    current.severity === "success" ? c.accent
    : current.severity === "warn" ? c.warn
    : current.severity === "danger" ? c.danger
    : c.blue;

  return (
    <Animated.View
      pointerEvents="box-none"
      style={{
        position: "absolute",
        left: space.lg,
        right: space.lg,
        bottom: space.xxl + 60,  // clear bottom tabs
        zIndex: 999,
      }}
    >
      <Pressable
        onPress={dismiss}
        style={{
          backgroundColor: c.surface2,
          borderColor: tint,
          borderWidth: 1,
          borderRadius: radius.md,
          paddingVertical: space.md,
          paddingHorizontal: space.lg,
          flexDirection: "row",
          alignItems: "center",
          gap: space.lg,
        }}
      >
        <Text style={[type.body, { color: c.text, flex: 1 }]}>{current.text}</Text>
        {current.action && (
          <Pressable
            onPress={() => {
              current.action?.onPress();
              dismiss();
            }}
            hitSlop={8}
          >
            <Text style={[type.h2, { color: tint, fontSize: 14 }]}>
              {current.action.label}
            </Text>
          </Pressable>
        )}
      </Pressable>
    </Animated.View>
  );
}
