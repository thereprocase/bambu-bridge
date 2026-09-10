/**
 * Add a printer — three-fork onboarding (contract §3).
 *
 * User supplies host + 8-digit access code + optional friendly name.
 * Bridge derives the serial from the leaf cert; classifies any failure
 * into E1 (unreachable) / E2 (auth) / E3 (silent). The envelope's
 * `message` + `remediation_hint` render directly — copy lives bridge-side.
 */

import { useRouter } from "expo-router";
import { useState } from "react";
import { ScrollView, Text } from "react-native";

import { BridgeError, BridgeNetworkError } from "../src/api/errors";
import { registerPrinter } from "../src/api/printers";
import { Button } from "../src/components/Button";
import { Field } from "../src/components/Field";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import { usePrintersStore } from "../src/store/printers";
import { useTheme } from "../src/theme/ThemeProvider";

export default function AddPrinter() {
  const router = useRouter();
  const { c, space, type } = useTheme();
  const showToast = useToastStore((s) => s.show);
  const refresh = usePrintersStore((s) => s.refresh);

  const [host, setHost] = useState("");
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [envelopeError, setEnvelopeError] = useState<null | {
    title: string; remediation?: string; phase?: string; serial?: string;
  }>(null);

  async function submit() {
    setEnvelopeError(null);
    setSubmitting(true);
    try {
      const res = await registerPrinter({
        host: host.trim(),
        access_code: code.trim(),
        friendly_name: name.trim() || undefined,
      });
      await refresh();
      showToast(`Connected to ${res.friendly_name}`, { severity: "success" });
      router.back();
    } catch (e) {
      if (e instanceof BridgeError) {
        setEnvelopeError({
          title: e.envelope.message,
          remediation: e.envelope.remediation_hint,
          phase: (e.envelope.context?.phase as string) ?? e.envelope.error,
          serial: e.envelope.discovered?.serial,
        });
      } else if (e instanceof BridgeNetworkError) {
        setEnvelopeError({ title: e.message });
      } else {
        setEnvelopeError({ title: String((e as Error)?.message ?? e) });
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      keyboardShouldPersistTaps="handled"
    >
      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h1, { color: c.text }]}>Add a Bambu P1S</Text>
        <Text style={[type.small, { color: c.muted }]}>
          The bridge needs the printer&apos;s LAN IP and the 8-digit access code
          shown at Settings ▸ WLAN. LAN-Only Mode must be on
          (Settings ▸ Network).
        </Text>

        <Field
          label="Host (IP)"
          value={host}
          onChangeText={setHost}
          placeholder="192.168.1.50"
          keyboardType="numeric"
        />
        <Field
          label="Access code"
          value={code}
          onChangeText={(t) => setCode(t.replace(/[^0-9]/g, "").slice(0, 8))}
          placeholder="12345678"
          keyboardType="number-pad"
          maxLength={8}
        />
        <Field
          label="Friendly name (optional)"
          value={name}
          onChangeText={setName}
          placeholder="Workshop P1S"
        />

        <Button
          label={submitting ? "Probing…" : "Probe & connect"}
          onPress={submit}
          loading={submitting}
          disabled={host.length === 0 || code.length !== 8}
          fullWidth
        />
      </Surface>

      {envelopeError && (
        <Surface padded style={{ gap: space.sm, borderColor: c.danger }}>
          <Text style={[type.h2, { color: c.danger }]}>
            {envelopeError.phase === "tls_handshake" ? "Can't reach the printer"
              : envelopeError.phase === "mqtt_connack" ? "Printer rejected the access code"
              : envelopeError.phase === "mqtt_no_telemetry" ? "Printer connected but silent"
              : "Couldn't add this printer"}
          </Text>
          <Text style={[type.body, { color: c.text }]}>{envelopeError.title}</Text>
          {envelopeError.serial && (
            <Text style={[type.caption, { color: c.muted }]}>
              Printer serial detected: {envelopeError.serial}
            </Text>
          )}
          {envelopeError.remediation && (
            <Text style={[type.small, { color: c.muted, marginTop: space.sm }]}>
              {envelopeError.remediation}
            </Text>
          )}
        </Surface>
      )}
    </ScrollView>
  );
}
