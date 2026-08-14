import { create } from 'zustand';

export type LabelEntry = { behaviour: string; confidence: number; ts: string };

interface LabelsStore {
  labels: Record<string, LabelEntry>;
  setLabel: (cageId: string, entry: LabelEntry) => void;
}

export const useLabels = create<LabelsStore>((set) => ({
  labels: {},
  setLabel: (cageId, entry) =>
    set((s) => ({ labels: { ...s.labels, [cageId]: entry } })),
}));
