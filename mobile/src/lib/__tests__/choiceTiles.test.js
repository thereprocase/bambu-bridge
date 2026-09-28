import React from "react";
import { jest, test, expect } from "@jest/globals";
import renderer, { act } from "react-test-renderer";
import { Pressable } from "react-native";
import { ChoiceTiles, tileWidth } from "../../components/ChoiceTiles";

jest.mock("react-native", () => ({
  View: "View", Text: "Text", ScrollView: "ScrollView", Pressable: "Pressable",
  useWindowDimensions: () => ({ fontScale: 1 }),
}));

jest.mock("../../theme/ThemeProvider", () => ({
  useTheme: () => ({
    c: jest.requireActual("../../theme/tokens").light,
    type: jest.requireActual("../../theme/tokens").typography,
  }),
}));

test("five equal targets fit a 320dp phone content area", () => {
  const contentWidth = 320 - 64;
  const width = tileWidth(contentWidth, 5, 1);
  expect(width).toBeGreaterThanOrEqual(48);
  expect(width * 5 + 4 * 4).toBe(contentWidth);
});
test("large text expands the scrollable row instead of shrinking targets", () => {
  expect(tileWidth(256, 5, 2)).toBe(96);
  expect(tileWidth(200, 5, 1)).toBe(48);
});
test("selection and disabled semantics survive the layout change", () => {
  const onSelect = jest.fn();
  let tree;
  act(() => { tree = renderer.create(<ChoiceTiles label="AMS slot" value="auto" onSelect={onSelect}
    options={[{ id: "auto", topLabel: "↻", label: "Auto" },
      { id: "1", color: "#000000", label: "1", accessibilityLabel: "AMS slot 1" },
      { id: "2", label: "2", disabled: true }]} />); });
  const buttons = tree.root.findAllByType(Pressable);
  expect(buttons).toHaveLength(3);
  expect(buttons[0].props.accessibilityState.checked).toBe(true);
  expect(buttons[1].props.accessibilityLabel).toBe("AMS slot 1");
  expect(buttons[2].props.disabled).toBe(true);
  act(() => buttons[1].props.onPress());
  expect(onSelect).toHaveBeenCalledWith("1");
  expect(buttons[1].props.style({ pressed: false }).minHeight).toBe(72);
  act(() => tree.unmount());
});
