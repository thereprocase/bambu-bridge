/**
 * How many prints wait for the owner. Written by whoever last read the
 * bridge's status (the tab layout's poll, the Filament tab's refresh, a
 * card closing) so the Filament tab badge is right without waiting a minute.
 */
import { create } from "zustand";

interface ReviewStore {
  count: number;
  setCount: (count: number) => void;
}

export const useReviewStore = create<ReviewStore>((set) => ({
  count: 0,
  setCount: (count) => set({ count }),
}));
