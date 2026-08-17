import { useState, useRef, useEffect, useCallback, forwardRef, useImperativeHandle } from 'react';
import { WS_URL } from '../config';

const TOPICS = {
  cnc:   { pub: 'machine/cnc/command',   sub: 'machine/cnc/response' },
  crane: { pub: 'machine/crane/command', sub: 'machine/crane/response' },
};

const S = {
  page:  { display: 'flex', gap: 20, padding: 20, height: '100%', boxSizing: 'border-box', overflow: 'hidden', fontFamily: 'var(--font-body)', background: 'var(--bg)' },
  panel: { display: 'flex', flexDirection: 'column', flex: 1, gap: 12, minWidth: 0, background: 'var(--panel)', border: '1px solid var(--line)', borderTop: '3px solid var(--accent)', borderRadius: 8, padding: 14 },
  title: { margin: 0, display: 'flex', alignItems: 'center', fontFamily: 'var(--font-display)', fontSize: 16, fontWeight: 700, letterSpacing: '1.5px', color: 'var(--text)', textTransform: 'uppercase', borderBottom: '1px solid var(--line)', paddingBottom: 8 },
  log:   { flex: 1, background: '#1d232c', color: '#9aa5b1', fontFamily: 'var(--font-mono)', fontSize: 12, padding: 12, border: '1px solid var(--line)', borderRadius: 8, overflowY: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all' },
  row:   { display: 'flex', gap: 8 },
  input: { flex: 1, fontFamily: 'var(--font-mono)', fontSize: 13, padding: '7px 10px', border: '1px solid var(--line)', background: 'var(--bg)', color: 'var(--text)', borderRadius: 8, outline: 'none' },
  btn:   { background: 'var(--accent)', color: '#ffffff', border: 'none', borderRadius: 8, padding: '7px 16px', fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 14, letterSpacing: '1px', textTransform: 'uppercase', cursor: 'pointer' },
  btnRed: { background: 'transparent', color: 'var(--text-dim)', border: '1px solid var(--line)' },
  dot:   (ok) => ({ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: ok ? 'var(--green)' : 'var(--text-faint)', boxShadow: ok ? '0 0 5px rgba(29,158,82,0.6)' : 'none', marginRight: 8 }),
};

function useDevWS(onMessage) {
  const ws = useRef(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    function connect() {
      const sock = new WebSocket(WS_URL);
      ws.current = sock;
      sock.onopen  = () => setConnected(true);
      sock.onclose = () => { setConnected(false); setTimeout(connect, 2000); };
      sock.onerror = () => sock.close();
      sock.onmessage = (e) => {
        try { onMessage(JSON.parse(e.data)); } catch {}
      };
    }
    connect();
    return () => { ws.current?.close(); };
  }, [onMessage]);

  const publish = useCallback((topic, payload) => {
    if (ws.current?.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify({ topic, payload }));
    }
  }, []);

  return { connected, publish };
}

const SerialPanel = forwardRef(function SerialPanel({ title, device, publish, connected }, ref) {
  const [input, setInput]   = useState('');
  const [log, setLog]       = useState([]);
  const logRef              = useRef();
  const topics              = TOPICS[device];

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  const addLog = useCallback((line, color) => setLog(l => [...l, { line, color, key: Date.now() + Math.random() }]), []);

  useImperativeHandle(ref, () => ({ addLog }), [addLog]);

  const send = () => {
    const cmd = input.trim();
    if (!cmd || !connected) return;
    addLog(`> ${cmd}`, '#fff');
    publish(topics.pub, { command: cmd });
    setInput('');
  };

  const handleKey = (e) => { if (e.key === 'Enter') send(); };

  return (
    <div style={S.panel}>
      <h2 style={S.title}>
        <span style={S.dot(connected)} />
        {title}
        <span style={{ fontWeight: 400, fontSize: 11, marginLeft: 8, color: '#999' }}>
          {connected ? topics.pub : 'disconnected'}
        </span>
      </h2>
      <div ref={logRef} style={S.log}>
        {log.length === 0 && <span style={{ color: '#555' }}>No output yet</span>}
        {log.map(entry => <div key={entry.key} style={{ color: entry.color }}>{entry.line}</div>)}
      </div>
      <div style={S.row}>
        <input
          style={S.input}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKey}
          placeholder={connected ? 'Enter command...' : 'Waiting for MQTT connection...'}
          disabled={!connected}
          spellCheck={false}
        />
        <button style={S.btn} onClick={send} disabled={!connected}>Send</button>
        <button style={{ ...S.btn, ...S.btnRed }} onClick={() => setLog([])}>Clear</button>
      </div>
    </div>
  );
});

export default function DevPage() {
  const cncRef   = useRef();
  const craneRef = useRef();

  const handleMessage = useCallback((msg) => {
    if (msg.topic === TOPICS.cnc.sub)   cncRef.current?.addLog(msg.payload?.response ?? JSON.stringify(msg.payload), '#0f0');
    if (msg.topic === TOPICS.crane.sub) craneRef.current?.addLog(msg.payload?.response ?? JSON.stringify(msg.payload), '#0f0');
  }, []);

  const { connected, publish } = useDevWS(handleMessage);

  return (
    <div style={S.page}>
      <SerialPanel ref={cncRef}   title="CNC (Gcode)" device="cnc"   publish={publish} connected={connected} />
      <SerialPanel ref={craneRef} title="Crane Arduino" device="crane" publish={publish} connected={connected} />
    </div>
  );
}
