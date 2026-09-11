import { Stack, useRouter } from "expo-router";
import { useRef, useState } from "react";
import { ScrollView, Text } from "react-native";
import { Button } from "../src/components/Button";
import { Field } from "../src/components/Field";
import { Surface } from "../src/components/Surface";
import { registerQaSecret } from "../src/lib/qalog";
import { claimPairing, scanPairingCode } from "../src/pairing/native";
import { parseInvitation, parseProfile, type Invitation } from "../src/pairing/protocol";
import { useBridgeStore } from "../src/store/bridge";
import { useTheme } from "../src/theme/ThemeProvider";

export default function PairScreen() {
  const { c, type, space } = useTheme();
  const router = useRouter();
  const [invitation, setInvitation] = useState<Invitation | null>(null);
  const [code, setCode] = useState("");
  const [name, setName] = useState("My phone");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const scanned = useRef(false);

  function read(value: string) {
    if (scanned.current) return;
    scanned.current = true; setError(null);
    try {
      const parsed = parseInvitation(value);
      registerQaSecret(parsed.secret); registerQaSecret(value);
      setInvitation(parsed); setCode("");
    } catch (e) { setError((e as Error).message); scanned.current = false; }
  }

  async function connect() {
    if (!invitation || busy) return;
    setBusy(true); setError(null);
    let claimed = false;
    try {
      const label = name.trim();
      if (!label || label.length > 64 || /[\x00-\x1f\x7f]/.test(label)) {
        throw new Error("Enter a phone name of 1–64 characters.");
      }
      const result = JSON.parse(await claimPairing(invitation.base_url, invitation.spki,
        invitation.secret, label));
      claimed = true;
      registerQaSecret(result.token);
      const profile = parseProfile(JSON.stringify({ version: 1, baseUrl: invitation.base_url,
        spki: invitation.spki, token: result.token, deviceId: result.device_id, name: label }));
      await useBridgeStore.getState().activatePairing(profile);
      router.replace("/(tabs)/status");
    } catch (e) {
      const nativeCode = (e as { code?: string })?.code;
      setError(claimed ? "Pairing succeeded, but the phone couldn't save it. Unlock the phone and create a new pairing code. Remove the unused device on the bridge."
        : nativeCode === "PAIR_IDENTITY" ? "The bridge identity didn't match the code. Get a fresh code directly from your bridge."
        : nativeCode === "PAIR_REJECTED" ? "This code expired or was already used. Create a new code."
        : nativeCode ? "Couldn't reach the bridge. Connect to your home Wi-Fi and try again."
        : e instanceof Error ? e.message : "Couldn't pair this phone.");
    } finally { setBusy(false); }
  }

  return <ScrollView style={{ flex: 1, backgroundColor: c.bg }} contentContainerStyle={{ padding: space.lg, gap: space.lg }}>
    <Stack.Screen options={{ title: "Pair your bridge" }} />
    <Surface padded style={{ gap: space.md }}>
      <Text style={[type.h1, { color: c.text }]}>Connect once. Stay private.</Text>
      <Text style={[type.body, { color: c.muted }]}>Connect to your home Wi-Fi and scan the pairing code displayed by your bridge installer. Your phone will verify the bridge before connecting.</Text>
      {!invitation && <>
        <Button label="Scan QR code" onPress={async () => {
          try {
            scanned.current = false; setError(null);
            const value = await scanPairingCode();
            if (value) read(value);
          } catch { setError("Couldn't open the camera. You can paste the pairing code below."); }
        }} fullWidth />
        <Field label="Or paste the pairing code" value={code} onChangeText={setCode}
          autoCorrect={false} autoCapitalize="none" secureTextEntry />
        <Button label="Read pairing code" variant="secondary" onPress={() => read(code)} disabled={!code.trim()} fullWidth />
      </>}
      {invitation && <>
        <Text style={[type.body, { color: c.text }]}>Ready to pair with the bridge shown on your setup screen.</Text>
        <Field label="Name this phone" value={name} onChangeText={setName} maxLength={64} />
        <Button label="Pair securely" onPress={connect} loading={busy} fullWidth />
        <Button label="Use another code" variant="secondary" disabled={busy} onPress={() => {
          setInvitation(null); scanned.current = false; setError(null);
        }} fullWidth />
      </>}
      {error && <Text accessibilityRole="alert" style={[type.body, { color: c.danger }]}>{error}</Text>}
    </Surface>
  </ScrollView>;
}
