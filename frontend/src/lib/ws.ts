import { useEffect, useRef, useState } from 'react';
import { WS_URL, getToken } from './api';

export type WsMessage =
  | { type: 'reading'; cage_id: string; sensor: string; ts: string; payload: Record<string, any> }
  | { type: 'alert'; cage_id: string; alert_id: number; severity: string; rule: string; message: string; ts: string }
  | { type: 'label'; cage_id: string; ts: string; behaviour: string; confidence: number };

export function useWebSocket(onMessage: (m: WsMessage) => void, enabled = true) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const token = getToken();
    if (!token) return;
    let cancelled = false;

    const connect = () => {
      const ws = new WebSocket(`${WS_URL}?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;
      ws.onopen = () => !cancelled && setConnected(true);
      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        setTimeout(connect, 1500);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          onMessage(data);
        } catch {
          /* ignore */
        }
      };
    };
    connect();
    return () => {
      cancelled = true;
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  return { connected };
}
