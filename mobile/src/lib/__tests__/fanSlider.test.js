import React from "react";
import { jest, test, expect } from "@jest/globals";
import renderer, { act } from "react-test-renderer";
import { FanSlider, fanPercent } from "../../components/FanSlider";
jest.mock("react-native", () => ({ View: "View", Text: "Text" }));
jest.mock("@react-native-community/slider", () => "Slider");
jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => ({ c: jest.requireActual("../../theme/tokens").light,
    type: jest.requireActual("../../theme/tokens").typography }),
}));
test.each([[0,0], [2,0], [3,5], [27,25], [28,30], [99,100], [105,100], [-5,0]])(
  "%s rounds and clamps to %s percent", (input, output) => expect(fanPercent(input)).toBe(output));
test("dragging previews values; release sends one five-percent command", async () => {
  const onSet = jest.fn().mockResolvedValue(undefined);
  let tree;
  act(() => { tree = renderer.create(<FanSlider label="Part fan" current={30} onSet={onSet} />); });
  let slider = tree.root.findByType("Slider");
  expect(slider.props.step).toBe(5);
  expect(slider.props.value).toBe(30);
  act(() => { slider.props.onSlidingStart(30); slider.props.onValueChange(47); });
  expect(onSet).not.toHaveBeenCalled();
  slider = tree.root.findByType("Slider");
  await act(async () => { slider.props.onSlidingComplete(47); slider.props.onSlidingComplete(47); });
  expect(onSet).toHaveBeenCalledTimes(1);
  expect(onSet).toHaveBeenCalledWith(45);
  act(() => tree.unmount());
});
test("unknown fan telemetry keeps the slider disabled", () => {
  let tree;
  act(() => { tree = renderer.create(<FanSlider label="Part fan" current={null} onSet={jest.fn()} />); });
  expect(tree.root.findByType("Slider").props.disabled).toBe(true);
  act(() => tree.unmount());
});
