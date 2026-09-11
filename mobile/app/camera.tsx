import { Stack, useLocalSearchParams } from "expo-router";
import { Text, View } from "react-native";
import { Camera } from "../src/viewing/Camera";
import { useViewingScreen } from "../src/viewing/screen";

export default function CameraScreen() {
  const params = useLocalSearchParams<{ printer?: string }>();
  useViewingScreen();
  return <View style={{ flex: 1, backgroundColor: "#111113" }}>
    <Stack.Screen options={{ title: "Camera", headerTintColor: "#fff", headerStyle: { backgroundColor: "#111113" } }} />
    {typeof params.printer === "string" ? <Camera printer={params.printer} fullscreen />
      : <Text style={{ color: "white", padding: 24 }}>Choose a printer first.</Text>}
  </View>;
}
