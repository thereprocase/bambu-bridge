import { useFocusEffect } from "expo-router";
import { useCallback, useState } from "react";
import { Text, View } from "react-native";

import { request } from "../api/client";
import { Button } from "../components/Button";
import { useToastStore } from "../components/Toast";
import { useTheme } from "../theme/ThemeProvider";

type Preview = { state: string; frames: number; total_frames: number };

export function previewLabel(value: Preview): string {
  if (value.state === "ready") return value.frames >= value.total_frames
    ? "Spinner ready" : `Rendering spinner · ${value.frames}/${value.total_frames}`;
  return ({ idle: "Preview available with a job", loading: "Loading preview",
    retrying: "Retrying preview", source_unavailable: "Source unavailable · retrying",
    preview_error: "Preview error" } as Record<string, string>)[value.state] ?? "Loading preview";
}

export function PreviewStatus({ printerId }: { printerId: string }) {
  const { c, type, space } = useTheme();
  const [value, setValue] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const show = useToastStore((s) => s.show);
  const path = `/printers/${encodeURIComponent(printerId)}/viz/preview`;
  useFocusEffect(useCallback(() => {
    let active = true, pending = false;
    setValue(null);
    const load = async () => {
      if (pending) return;
      pending = true;
      try { const next = await request<Preview>(path); if (active) setValue(next); }
      catch { if (active) setValue(null); }
      finally { pending = false; }
    };
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => { active = false; clearInterval(timer); };
  }, [path]));
  if (!value) return null;
  const retryable = ["retrying", "source_unavailable", "preview_error"].includes(value.state);
  return <View style={{ gap: space.sm }}>
    <Text style={[type.small, { color: c.muted }]}>{previewLabel(value)}</Text>
    {retryable && <Button label="Retry preview" disabled={busy} onPress={async () => {
      setBusy(true);
      try { setValue(await request<Preview>(`${path}/retry`, { method: "POST" })); }
      catch { show("Preview retry failed", { severity: "danger" }); }
      finally { setBusy(false); }
    }} />}
  </View>;
}
