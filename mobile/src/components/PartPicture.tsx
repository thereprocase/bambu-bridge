/**
 * The slicer's picture of a captured print, from the bridge's library.
 * Renders nothing until the URL resolves and nothing at all when the bridge
 * has no picture (older bridges answer 404), so the card never shows a
 * broken frame.
 */

import { Image } from "expo-image";
import { useEffect, useState } from "react";

import { resolveBaseUrl } from "../api/endpoint";
import { useBridgeStore } from "../store/bridge";
import { useTheme } from "../theme/ThemeProvider";

export function PartPicture({ captureId, height = 160 }: { captureId: string | null; height?: number }) {
  const { c, radius } = useTheme();
  const bearer = useBridgeStore((s) => s.bearer);
  const [uri, setUri] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setFailed(false);
    setUri(null);
    if (!captureId) return;
    void resolveBaseUrl()
      .then((base) => {
        if (!active || !base) return;
        setUri(`${base.replace(/\/+$/, "")}/library/captures/${encodeURIComponent(captureId)}/thumbnail`);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [captureId]);

  if (!uri || !bearer || failed) return null;
  return (
    <Image
      source={{ uri, headers: { Authorization: `Bearer ${bearer}` } }}
      style={{ width: "100%", height, borderRadius: radius.md, backgroundColor: c.surface2 }}
      contentFit="contain"
      accessibilityLabel="Picture of the printed part"
      onError={() => setFailed(true)}
    />
  );
}
