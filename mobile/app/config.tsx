/** Legacy deep links open Settings; URL parameters never alter saved credentials or data. */
import { Redirect } from "expo-router";

export default function ConfigRoute() {
  return <Redirect href="/settings" />;
}
