import ConfigRoute from "../../../app/config";
import { Redirect } from "expo-router";

jest.mock("expo-router", () => ({ Redirect: () => null,
  useLocalSearchParams: () => { throw new Error("must not read credential or reset parameters"); } }));

it("opens Settings without consuming deep-link credentials or reset commands", () => {
  const screen = ConfigRoute();
  expect(screen.type).toBe(Redirect);
  expect(screen.props).toEqual({ href: "/settings" });
});
