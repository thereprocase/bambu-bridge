/**
 * Registered-printers store. Refetches `GET /printers` and maintains the
 * "currently selected" id (which the tabs key off of). Selection persists
 * across launches so the user comes back to whatever they were watching.
 */

import { create } from "zustand";

import { listPrinters } from "../api/printers";
import { PrinterSummary } from "../api/types";
import { kv } from "../lib/kv";

const KV_SELECTED = "printers.selectedId";

interface PrintersStore {
  list: PrinterSummary[];
  selectedId: string | null;
  loading: boolean;
  error: string | null;
  /** Epoch ms of the last *successful* `GET /printers`; 0 = never. */
  lastFetched: number;

  refresh(): Promise<void>;
  select(id: string | null): void;
}

export const usePrintersStore = create<PrintersStore>((set, get) => ({
  list: [],
  selectedId: kv.getString(KV_SELECTED) ?? null,
  loading: false,
  error: null,
  lastFetched: 0,

  refresh: async () => {
    set({ loading: true, error: null });
    try {
      const list = await listPrinters();
      // If the previously-selected id was deleted out from under us, pick
      // the first registered printer (or clear if none).
      const cur = get().selectedId;
      const selectedId =
        cur && list.some((p) => p.serial === cur)
          ? cur
          : (list[0]?.serial ?? null);
      if (selectedId !== cur) {
        if (selectedId) kv.set(KV_SELECTED, selectedId);
        else kv.remove(KV_SELECTED);
      }
      set({ list, selectedId, loading: false, lastFetched: Date.now() });
    } catch (e) {
      set({ loading: false, error: String((e as Error)?.message ?? e) });
    }
  },

  select: (id) => {
    if (id) kv.set(KV_SELECTED, id);
    else kv.remove(KV_SELECTED);
    set({ selectedId: id });
  },
}));
