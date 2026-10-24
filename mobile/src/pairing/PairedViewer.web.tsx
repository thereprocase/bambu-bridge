import { Text, View, type ViewProps } from "react-native";
import { useTheme } from "../theme/ThemeProvider";

interface Props extends ViewProps {
  uri: string;
  onMessage: (event: { nativeEvent: { data: string } }) => void;
  onError: (event: { nativeEvent: { reason?: string } }) => void;
  onLoad: () => void;
}

// The paired viewer is a native view (PairedBridgeViewer); browsers show a notice.
export function PairedViewer({ style }: Props) {
  const { c, type } = useTheme();
  return (
    <View style={style}>
      <Text style={[type.body, { color: c.text }]}>
        The paired viewer is available in the Android app.
      </Text>
    </View>
  );
}
