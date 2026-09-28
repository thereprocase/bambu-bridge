import Slider from "@react-native-community/slider";
import { useRef, useState } from "react";
import { Text, View } from "react-native";
import { useTheme } from "../theme/ThemeProvider";

export const fanPercent = (value: number) => Math.max(0, Math.min(100, Math.round(value / 5) * 5));

export function FanSlider({ label, current, onSet }: {
  label: string; current: number | null; onSet: (percent: number) => Promise<unknown>;
}) {
  const { c, type } = useTheme();
  const [draft, setDraft] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sending = useRef(false);
  const dragging = useRef(false);
  const start = useRef(0);
  return <View style={{ gap: 4 }}>
    <View style={{ flexDirection: "row", justifyContent: "space-between", gap: 8 }}>
      <Text style={[type.body, { color: c.text, flexShrink: 1 }]}>{label}</Text>
      <Text style={[type.mono, { color: c.text }]}>
        {draft != null ? `${draft}%${busy ? " · Sending" : ""}` : current == null ? "Unknown" : `${Math.round(current)}%`}
      </Text>
    </View>
    <Slider style={{ height: 48, width: "100%" }}
      accessibilityLabel={label}
      minimumValue={0} maximumValue={100} step={5}
      value={dragging.current ? start.current : current ?? 0}
      disabled={busy || current == null}
      minimumTrackTintColor={c.accent} maximumTrackTintColor={c.border} thumbTintColor={c.accent}
      onSlidingStart={value => { start.current = value; dragging.current = true; setDraft(fanPercent(value)); }}
      onValueChange={value => setDraft(fanPercent(value))}
      onSlidingComplete={value => {
        dragging.current = false;
        if (sending.current || current == null) return;
        const percent = fanPercent(value);
        sending.current = true;
        setError(""); setDraft(percent); setBusy(true);
        void Promise.resolve().then(() => onSet(percent))
          .catch(() => setError("Fan command failed. Try again."))
          .finally(() => { sending.current = false; setBusy(false); setDraft(null); });
      }} />
    <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
      <Text style={[type.caption, { color: c.muted }]}>0%</Text>
      <Text style={[type.caption, { color: c.muted }]}>100%</Text>
    </View>
    {!!error && <Text accessibilityRole="alert" style={[type.small, { color: c.danger }]}>{error}</Text>}
  </View>;
}
