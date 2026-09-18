import { create } from 'zustand';
import { cartAPI } from '../services/api';

export interface CartItem {
  id: number;
  component_id: number;
  distributor_id: number;
  quantity: number;
  unit_price: number | null;
  mpn: string | null;
  manufacturer: string | null;
  category: string | null;
  distributor_name: string | null;
  distributor_city: string | null;
  distributor_state: string | null;
  distributor_country: string | null;
}

interface CartState {
  items: CartItem[];
  loading: boolean;
  error: string | null;
  fetchCart: () => Promise<void>;
  addItem: (data: { component_id: number; distributor_id: number; quantity: number; unit_price?: number }) => Promise<void>;
  removeItem: (id: number) => Promise<void>;
  clearCart: () => Promise<void>;
}

/** Pull a human-readable message off an axios error without trusting its shape. */
function errorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const joined = detail
      .map((d) => (typeof d === 'string' ? d : (d as { msg?: string })?.msg))
      .filter(Boolean)
      .join(', ');
    if (joined) return joined;
  }
  return fallback;
}

export const useCartStore = create<CartState>((set) => ({
  items: [],
  loading: false,
  error: null,

  fetchCart: async () => {
    set({ loading: true, error: null });
    try {
      const res = await cartAPI.get();
      set({ items: res.data, loading: false });
    } catch (err: unknown) {
      set({ loading: false, error: errorMessage(err, 'Failed to load cart') });
    }
  },

  addItem: async (data) => {
    try {
      await cartAPI.add(data);
      const res = await cartAPI.get();
      set({ items: res.data, error: null });
    } catch (err: unknown) {
      const message = errorMessage(err, 'Failed to add item');
      set({ error: message });
      throw new Error(message);
    }
  },

  // Server-first, like fetchCart/addItem above: only mutate local state after the
  // API confirms. A failure used to escape as an unhandled rejection and leave the
  // UI claiming the item was gone when the server still had it.
  removeItem: async (id) => {
    try {
      await cartAPI.remove(id);
      set((s) => ({ items: s.items.filter((i) => i.id !== id), error: null }));
    } catch (err: unknown) {
      const message = errorMessage(err, 'Failed to remove item from cart');
      set({ error: message });
      throw new Error(message);
    }
  },

  clearCart: async () => {
    try {
      await cartAPI.clear();
      set({ items: [], error: null });
    } catch (err: unknown) {
      const message = errorMessage(err, 'Failed to clear cart');
      set({ error: message });
      throw new Error(message);
    }
  },
}));
