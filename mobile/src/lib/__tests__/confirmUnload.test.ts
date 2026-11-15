import { Alert } from "react-native";

import { confirmUnload } from "../../components/confirmUnload";

type AlertButton = { text?: string; style?: string; onPress?: () => void };

it("sends the unload only after the person confirms", () => {
  const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  const unload = jest.fn();
  confirmUnload(unload);
  expect(unload).not.toHaveBeenCalled();
  const [title, , buttons] = alert.mock.calls[0] as [string, string, AlertButton[]];
  expect(title).toBe("Unload filament?");
  const cancel = buttons.find((b) => b.style === "cancel")!;
  expect(cancel.onPress).toBeUndefined();
  buttons.find((b) => b.text === "Unload")!.onPress!();
  expect(unload).toHaveBeenCalledTimes(1);
});
