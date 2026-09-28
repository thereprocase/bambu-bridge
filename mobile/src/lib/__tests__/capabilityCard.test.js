import React from "react";
import { jest, test, expect } from "@jest/globals";
import renderer, { act } from "react-test-renderer";
import { CapabilityCard, CapabilityList } from "../../components/CapabilityCard";
jest.mock("react-native", () => ({ Text: "Text", View: "View" }));
jest.mock("../../components/Surface", () => ({ Surface: ({ children }) => <surface>{children}</surface> }));
jest.mock("../../theme/ThemeProvider", () => ({ useTheme: () => ({ c: {}, type: {}, space: {} }) }));
test("unavailable cards move last and their buttons never mount", () => {
  const mounted = jest.fn();
  function Controls() { mounted(); return <button />; }
  let tree;
  act(() => { tree = renderer.create(<CapabilityList>
    <CapabilityCard title="Vision" availability={{ available: false, reason: "Not available on P1S" }}><Controls /></CapabilityCard>
    <label>Camera</label>
    {[<CapabilityCard key="sound" title="Sound" availability={{ available: false, reason: "Not available on P1S" }}><Controls /></CapabilityCard>]}
    <label>Temperatures</label>
  </CapabilityList>); });
  expect(mounted).not.toHaveBeenCalled();
  expect(tree.root.findAllByType("button")).toHaveLength(0);
  expect(tree.toJSON().map(n => n.type)).toEqual(["label", "label", "surface", "surface"]);
  expect(JSON.stringify(tree.toJSON())).toContain("Not available on P1S");
  act(() => tree.unmount());
});
