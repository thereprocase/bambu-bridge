import { Link } from "expo-router";
import { Switch, Text, View } from "react-native";
import { Button } from "../components/Button";
import { Surface } from "../components/Surface";
import { useTheme } from "../theme/ThemeProvider";
import { useViewingStore } from "./state";

export function ViewingSettings() {
  const { c, type, space } = useTheme();
  const awake = useViewingStore(s => s.keepAwake);
  const setAwake = useViewingStore(s => s.setKeepAwake);
  return <Surface padded style={{ gap: space.md }}>
    <Text style={[type.h2, { color: c.text }]}>Viewing & connection</Text>
    <View style={{ flexDirection: "row", alignItems: "center", gap: space.md }}>
      <Text style={[type.body, { flex: 1, color: c.text }]}>Keep screen awake in fullscreen camera and 3D</Text>
      <Switch accessibilityLabel="Keep screen awake while viewing" value={awake} onValueChange={setAwake} />
    </View>
    <Text style={[type.small, { color: c.muted }]}>Camera streams at the printer’s available rate. Fullscreen views rotate with your phone; pinch to zoom the camera. Enable print alerts on the Status screen.</Text>
    <Link href="/connection" asChild><Button label="Check connection" onPress={() => {}} fullWidth /></Link>
  </Surface>;
}
