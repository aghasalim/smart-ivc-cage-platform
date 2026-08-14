import { create } from 'zustand';
import { api, getToken, setToken } from '@/lib/api';

type User = { id: number; username: string; role: string };

type AuthState = {
  user: User | null;
  ready: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
};

export const useAuth = create<AuthState>((set) => ({
  user: null,
  ready: false,
  async login(username, password) {
    const r = await api<{ access_token: string }>('/api/v1/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    setToken(r.access_token);
    const me = await api<User>('/api/v1/auth/me');
    set({ user: me, ready: true });
  },
  logout() {
    setToken(null);
    set({ user: null, ready: true });
  },
  async refresh() {
    if (!getToken()) {
      set({ user: null, ready: true });
      return;
    }
    try {
      const me = await api<User>('/api/v1/auth/me');
      set({ user: me, ready: true });
    } catch {
      setToken(null);
      set({ user: null, ready: true });
    }
  },
}));
