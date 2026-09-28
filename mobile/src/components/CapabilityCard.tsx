import { Children, isValidElement, ReactNode } from "react";
import { Text } from "react-native";
import { Availability } from "../lib/capabilities";
import { useTheme } from "../theme/ThemeProvider";
import { Surface } from "./Surface";

export function CapabilityCard({ title, availability, children }: {
  title: string; availability: Availability; children: ReactNode;
}) {
  const { c, type, space } = useTheme();
  if (availability.available) return <>{children}</>;
  return <Surface padded style={{ gap: space.sm }}>
    <Text style={[type.h2, { color: c.muted }]}>{title}</Text>
    <Text style={[type.small, { color: c.muted }]}>{availability.reason}</Text>
  </Surface>;
}

/** Stable partition: active cards first, unavailable summaries last.
 * Children of unavailable cards never mount, including their touch targets.
 */
export function CapabilityList({ children }: { children: ReactNode }) {
  const items = Children.toArray(children);
  const unavailable = (child: ReactNode) => isValidElement<{ availability?: Availability }>(child)
    && child.props.availability?.available === false;
  return <>{items.filter((child) => !unavailable(child))}{items.filter(unavailable)}</>;
}
