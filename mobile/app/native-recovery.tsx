/** Native upload receipts and recovery for paired bridge administrators. */

import { useFocusEffect } from "expo-router";
import * as DocumentPicker from "expo-document-picker";
import * as Sharing from "expo-sharing";
import { useCallback, useRef, useState } from "react";
import { Alert, Pressable, RefreshControl, ScrollView, Text, View } from "react-native";

import { BridgeError } from "../src/api/errors";
import {
  acknowledgePastNativeReviews,
  createNativeBackup,
  deleteNativeBackup,
  exportNativeBackup,
  importNativeBackupFile,
  nativeAction,
  nativeBackups,
  nativeQueue,
  restoreNativeBackup,
  type NativeAction,
  type NativeBackup,
  type NativeQueuePage,
  type NativeReceipt,
  type NativeQueueView,
} from "../src/api/native";
import { Button } from "../src/components/Button";
import { ChoiceTiles } from "../src/components/ChoiceTiles";
import { Surface } from "../src/components/Surface";
import { useToastStore } from "../src/components/Toast";
import { fileLabel, ownerLabel, startLabel } from "../src/lib/nativeState";
import { useBridgeStore } from "../src/store/bridge";
import { useTheme } from "../src/theme/ThemeProvider";

const OWNER_STATES = new Set(["reserved", "queued", "dispatching", "sent", "accepted", "running", "unknown"]);
const RECEIPT_VIEWS: { id: NativeQueueView; label: string }[] = [
  { id: "review", label: "Needs review" },
  { id: "active", label: "In progress" },
  { id: "all", label: "All receipts" },
];

function detail(error: unknown): string {
  return error instanceof BridgeError ? error.envelope.message : String(error);
}

function actions(row: NativeReceipt): { action: NativeAction; label: string; explanation: string }[] {
  const result: { action: NativeAction; label: string; explanation: string }[] = [];
  if (row.start_state === "queued" || row.start_state === "reserved") {
    result.push({ action: "cancel", label: "Cancel pending start",
      explanation: "Cancel this pending print start." });
  }
  if (["unknown", "accepted", "sent", "running", "blocked", "cancelled", "rejected"].includes(row.start_state ?? "")) {
    result.push({ action: "resolve", label: "Resolve after checking printer",
      explanation: "Confirm the printer is idle, then clear this print-start record for review." });
  }
  if (row.state === "failed" && !!row.complete) {
    result.push({ action: "retry_delivery", label: "Retry file delivery",
      explanation: "Send the saved file to the printer again. Blocked print starts remain held for review." });
  }
  if (["failed", "delivered", "external"].includes(row.state) && !!row.retained) {
    result.push({ action: "discard", label: "Discard local file",
      explanation: "Delete the bridge's cached copy. Keep the file stored on the printer." });
  }
  return result;
}

export default function NativeRecoveryScreen() {
  const { c, space, type } = useTheme();
  const showToast = useToastStore((s) => s.show);
  const paired = useBridgeStore((s) => !!s.pairing);
  const [page, setPage] = useState<NativeQueuePage | null>(null);
  const [rows, setRows] = useState<NativeReceipt[]>([]);
  const [view, setView] = useState<NativeQueueView>("review");
  const viewRef = useRef(view);
  const refreshToken = useRef(0);
  const [backups, setBackups] = useState<NativeBackup[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const requestedView = view;
    const token = ++refreshToken.current;
    setLoading(true);
    try {
      const [queue, saved] = await Promise.all([nativeQueue(0, requestedView), nativeBackups()]);
      if (token !== refreshToken.current || viewRef.current !== requestedView) return;
      setPage(queue);
      setRows(queue.uploads);
      setBackups(saved);
      setMessage(null);
    } catch (error) {
      if (token === refreshToken.current && viewRef.current === requestedView) setMessage(detail(error));
    } finally {
      if (token === refreshToken.current) setLoading(false);
    }
  }, [view]);

  useFocusEffect(useCallback(() => { void refresh(); }, [refresh]));

  async function loadMore() {
    if (!page?.has_more || busy) return;
    setBusy("older");
    const requestedView = view;
    try {
      const next = await nativeQueue(rows.length, requestedView);
      if (viewRef.current !== requestedView) return;
      setRows((current) => [...current, ...next.uploads]);
      setPage(next);
    } catch (error) {
      showToast(detail(error), { severity: "danger" });
    } finally {
      setBusy(null);
    }
  }

  function confirmAction(row: NativeReceipt, action: NativeAction, label: string, explanation: string) {
    Alert.alert(label, `${explanation}\n\nReceipt ${row.id}`, [
      { text: "Keep", style: "cancel" },
      { text: label, style: "destructive", onPress: () => {
        setBusy(row.id);
        void nativeAction(row.id, action)
          .then(() => refresh())
          .catch((error) => showToast(detail(error), { severity: "danger" }))
          .finally(() => setBusy(null));
      } },
    ]);
  }

  function confirmAcknowledgePast() {
    const count = page?.acknowledgeable_count ?? 0;
    if (!count || busy) return;
    Alert.alert("Acknowledge past warnings?",
      `Acknowledge ${count} historical warning${count === 1 ? "" : "s"}. Keep the records, files, and current print-start states.`, [
        { text: "Keep reviewing", style: "cancel" },
        { text: "Acknowledge past", onPress: () => {
          setBusy("acknowledge");
          void acknowledgePastNativeReviews()
            .then(async ({ acknowledged }) => {
              showToast(`${acknowledged} past warning${acknowledged === 1 ? "" : "s"} acknowledged`, { severity: "success" });
              await refresh();
            })
            .catch((error) => showToast(detail(error), { severity: "danger" }))
            .finally(() => setBusy(null));
        } },
      ]);
  }

  async function makeBackup() {
    setBusy("backup");
    try {
      const saved = await createNativeBackup();
      showToast(`Inbox backup ${saved.id.slice(0, 8)} saved`, { severity: "success" });
      await refresh();
    } catch (error) {
      showToast(detail(error), { severity: "danger" });
    } finally {
      setBusy(null);
    }
  }

  function confirmRestore(saved: NativeBackup) {
    Alert.alert("Restore native inbox?",
      "Native connections will restart. Restored print starts will be held for review. The current inbox will be backed up first.", [
        { text: "Keep current inbox", style: "cancel" },
        { text: "Restore backup", style: "destructive", onPress: () => {
          setBusy(saved.id);
          void restoreNativeBackup(saved.id)
            .then(async () => {
              showToast("Inbox restored. Review pending starts before printing.", { severity: "success" });
              await refresh();
            })
            .catch((error) => showToast(detail(error), { severity: "danger" }))
            .finally(() => setBusy(null));
        } },
      ]);
  }

  function confirmDelete(saved: NativeBackup) {
    Alert.alert("Delete inbox backup?", "This removes the saved backup from the bridge. Keep an off-device copy if you may need it later.", [
      { text: "Keep backup", style: "cancel" },
      { text: "Delete", style: "destructive", onPress: () => {
        setBusy(saved.id);
        void deleteNativeBackup(saved.id)
          .then(() => refresh())
          .catch((error) => showToast(detail(error), { severity: "danger" }))
          .finally(() => setBusy(null));
      } },
    ]);
  }

  async function shareBackup(saved: NativeBackup) {
    setBusy(saved.id);
    try {
      const uri = await exportNativeBackup(saved.id);
      await Sharing.shareAsync(uri, { mimeType: "application/zip", dialogTitle: "Save native inbox backup" });
    } catch (error) {
      showToast(detail(error), { severity: "danger" });
    } finally {
      setBusy(null);
    }
  }

  async function importBackup() {
    try {
      const picked = await DocumentPicker.getDocumentAsync({
        type: ["application/zip", "application/octet-stream"], copyToCacheDirectory: true,
      });
      if (picked.canceled || !picked.assets[0]) return;
      setBusy("import");
      await importNativeBackupFile(picked.assets[0].uri);
      showToast("Backup imported and verified. Choose Restore when ready.", { severity: "success" });
      await refresh();
    } catch (error) {
      showToast(detail(error), { severity: "danger" });
    } finally {
      setBusy(null);
    }
  }

  return (
    <ScrollView style={{ flex: 1, backgroundColor: c.bg }}
      contentContainerStyle={{ padding: space.lg, gap: space.lg }}
      refreshControl={<RefreshControl refreshing={loading} onRefresh={() => { void refresh(); }} tintColor={c.accent} />}>
      <Surface padded style={{ gap: space.sm }}>
        <Text style={[type.h1, { color: c.text }]}>Print history and recovery</Text>
        <Text style={[type.body, { color: c.muted }]}>
          File transfers and print-start records.
        </Text>
        <Text style={[type.body, { color: c.text }]}>Gateway printer: {page?.printer_id || "Loading identity…"}</Text>
        {message && <Text style={[type.body, { color: c.danger }]}>{message}</Text>}
        {page && <Text style={[type.body, { color: c.text }]}>
          {ownerLabel(page)}. {" "}
          {page.review_count} receipts need review.
        </Text>}
        <Button label="Refresh receipts" variant="secondary" onPress={() => { void refresh(); }} loading={loading} />
        {!!page?.acknowledgeable_count && (
          <Button label={`Acknowledge past warnings (${page.acknowledgeable_count})`}
            variant="secondary" onPress={confirmAcknowledgePast} loading={busy === "acknowledge"} />
        )}
      </Surface>

      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Upload receipts</Text>
        <ChoiceTiles label="Receipt filter" value={view}
          options={RECEIPT_VIEWS.map(option => {
            const [topLabel, ...rest] = option.label.split(" ");
            return { id: option.id, topLabel, label: rest.join(" ") };
          })}
          onSelect={id => {
            const next = id as NativeQueueView;
            viewRef.current = next; setPage(null); setRows([]); setView(next);
          }} />
        {rows.length === 0 && !loading && <Text style={[type.body, { color: c.muted }]}>
          {view === "review" ? "No receipts need review." : view === "active" ? "Nothing in progress." : "No receipts."}
        </Text>}
        {rows.map((row) => (
          <View key={row.id} style={{ borderTopWidth: 1, borderTopColor: c.borderSoft, paddingTop: space.md, gap: space.sm }}>
            <Text style={[type.body, { color: c.text }]}>{row.logical || row.id}</Text>
            <Text style={[type.small, { color: c.muted }]}>
              File: {fileLabel(row)} · Start: {startLabel(row.start_state)}
            </Text>
            <Text style={[type.small, { color: c.muted }]}>
              {new Date(row.created * 1000).toLocaleString()} · {row.bytes} bytes · {row.id} · {row.code || "no code"}
              {OWNER_STATES.has(row.start_state ?? "") ? " · blocks another start" : ""}
              {row.review_ack_at ? " · acknowledged" : ""}
            </Text>
            {actions(row).map(({ action, label, explanation }) => (
              <Pressable key={action} disabled={!!busy} onPress={() => confirmAction(row, action, label, explanation)}>
                <Text style={[type.body, { color: c.accent }]}>{label}</Text>
              </Pressable>
            ))}
          </View>
        ))}
        {page?.has_more && <Button label="Load older receipts" variant="secondary" onPress={() => { void loadMore(); }} loading={busy === "older"} />}
      </Surface>

      <Surface padded style={{ gap: space.md }}>
        <Text style={[type.h2, { color: c.text }]}>Inbox backups</Text>
        <Text style={[type.small, { color: c.muted }]}>Includes inbox records and cached files. Bridge configuration, credentials and phone pairing require a separate system backup.</Text>
        <Text style={[type.body, { color: c.muted }]}>
          Backups include the receipt database and complete cached upload files. A restore restarts native access and leaves pending starts for review.
        </Text>
        <Button label="Create backup now" onPress={() => { void makeBackup(); }} loading={busy === "backup"} />
        {paired && <Button label="Import backup file" variant="secondary" onPress={() => { void importBackup(); }} loading={busy === "import"} />}
        {backups.length === 0 && <Text style={[type.small, { color: c.muted }]}>No backups saved on this bridge.</Text>}
        {backups.map((saved) => (
          <View key={saved.id} style={{ borderTopWidth: 1, borderTopColor: c.borderSoft, paddingTop: space.sm, gap: space.sm }}>
            <Text style={[type.body, { color: c.text }]}>{new Date(saved.created * 1000).toLocaleString()}</Text>
            <Text style={[type.small, { color: c.muted }]}>{saved.id} · {saved.bytes} bytes</Text>
            {paired && <Button label="Save a copy off the bridge" variant="secondary" onPress={() => { void shareBackup(saved); }} loading={busy === saved.id} />}
            <Button label="Restore this backup" variant="secondary" onPress={() => confirmRestore(saved)} loading={busy === saved.id} />
            <Button label="Delete backup" variant="secondary" onPress={() => confirmDelete(saved)} loading={busy === saved.id} />
          </View>
        ))}
        {!paired && <Text style={[type.small, { color: c.muted }]}>Pair this phone to import or export backup files securely.</Text>}
      </Surface>
    </ScrollView>
  );
}
