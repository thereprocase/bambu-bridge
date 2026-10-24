import { Text } from "react-native";
import { Surface } from "../components/Surface";
import { useTheme } from "../theme/ThemeProvider";

// Background print alerts run in the Android foreground service (BridgeViewing);
// browsers have no equivalent, so the web build states where alerts live.
export function MonitorControls(_props: { printer: string }) {
  const { c, type, space } = useTheme();
  return (
    <Surface padded style={{ gap: space.sm }}>
      <Text style={[type.h2, { color: c.text }]}>Print alerts</Text>
      <Text style={[type.small, { color: c.muted }]}>
        Background print alerts are available in the Android app.
      </Text>
    </Surface>
  );
}
