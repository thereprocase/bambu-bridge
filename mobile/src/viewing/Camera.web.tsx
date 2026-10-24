import { Text, View } from "react-native";
import { useTheme } from "../theme/ThemeProvider";

// The live camera is a native view (BridgeCameraView). The browser build keeps
// the surrounding screens usable and says where the camera is available.
export function Camera(_props: { printer: string; fullscreen?: boolean }) {
  const { c, type } = useTheme();
  return (
    <View style={{ padding: 12 }}>
      <Text style={[type.body, { color: c.text }]}>
        The live camera is available in the Android app.
      </Text>
    </View>
  );
}
