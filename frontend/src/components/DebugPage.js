import { useState, useRef, useEffect, useCallback } from 'react';
import { WS_URL } from '../config';

// One-tap buttons - each just sends its shell command string. The Pi runs the
// exact same dispatcher as `python main.py --debug`, so anything you can type
// in the shell works in the input box too (raw gcode included).
const GROUPS = [
  { label: 'carriage',  cmds: [['C1', 'c1'], ['C2', 'c2'], ['home $H', '$H']] },
  { label: 'jog x',     cmds: [['-5', 'x-----'], ['-1', 'x-'], ['+1', 'x+'], ['+5', 'x+++++']] },
  { label: 'jog y',     cmds: [['-5', 'y-----'], ['-1', 'y-'], ['+1', 'y+'], ['+5', 'y+++++']] },
  { label: 'drive',     cmds: [['fwd', 'f'], ['rev', 'r'], ['STOP', 's']] },
  { label: 'lifts',     cmds: [['up', 'u'], ['down', 'd']] },
  { label: 'depth',     cmds: [['1', 'depth 1'], ['2', 'depth 2'], ['3', 'depth 3'],
                               ['read', 'readpos']] },
  { label: 'sequences', cmds: [['locate', 'locate'], ['retrieve', 'ret'],
                               ['go home', 'gohome'], ['STORE', 'store']] },
  { label: 'read',      cmds: [['pos', 'pos'], ['home', 'home'], ['bin', 'bin'],
                               ['align', 'align'], ['current', 'cur'], ['dist', 'dist'],
                               ['scan', 'scan']] },
  { label: 'misc',      cmds: [['positions', 'positions'], ['sleep', 'sleep'],
                               ['wake', 'wake'], ['help', 'help']] },
];

const S = {
  page:  { display: 'flex', flexDirection: 'column', gap: 12, padding: 20, height: '100%', boxSizing: 'border-box', overflow: 'hidden', fontFamily: 'var(--font-body)', background: 'var(--bg)' },
  head:  { display: 'flex', alignItems: 'center', gap: 10, fontFamily: 'var(--font-display)', fontSize: 16, fontWeight: 700, letterSpacing: '1.5px', color: 'var(--text)', textTransform: 'uppercase' },
  dot:   (ok) => ({ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: ok ? 'var(--green)' : 'var(--text-faint)', boxShadow: ok ? '0 0 5px rgba(29,158,82,0.6)' : 'none' }),
  groups:{ display: 'flex', flexWrap: 'wrap', gap: 10 },
  group: { display: 'flex', alignItems: 'center', gap: 6, background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 8, padding: '6px 10px' },
  gname: { fontFamily: 'var(--font-display)', fontSize: 11, letterSpacing: '1px', textTransform: 'uppercase', color: 'var(--text-dim)', marginRight: 4 },
  btn:   { background: 'var(--bg)', color: 'var(--text)', border: '1px solid var(--line)', borderRadius: 6, padding: '6px 12px', fontFamily: 'var(--font-mono)', fontSize: 13, cursor: 'pointer', minWidth: 40 },
  stop:  { background: '#b3261e', color: '#fff', border: 'none', fontWeight: 700 },
  log:   { flex: 1, background: '#1d232c', color: '#9aa5b1', fontFamily: 'var(--font-mono)', fontSize: 12, padding: 12, border: '1px solid var(--line)', borderRadius: 8, overflowY: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all' },
  row:   { display: 'flex', gap: 8 },
  input: { flex: 1, fontFamily: 'var(--font-mono)', fontSize: 13, padding: '7px 10px', border: '1px solid var(--line)', background: 'var(--panel)', color: 'var(--text)', borderRadius: 8, outline: 'none' },
  send:  { background: 'var(--accent)', color: '#fff', border: 'none', borderRadius: 8, padding: '7px 16px', fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 14, letterSpacing: '1px', textTransform: 'uppercase', cursor: 'pointer' },
};

export default function DebugPage() {
  const ws = useRef(null);
  const logRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const [log, setLog] = useState([]);
  const [input, setInput] = useState('');

  const addLog = useCallback((text, color) => {
    setLog(prev => [...prev.slice(-499), { text, color, key: Date.now() + Math.random() }]);
  }, []);

  useEffect(() => {
    let alive = true;
    function connect() {
      if (!alive) return;
      const sock = new WebSocket(WS_URL);
      ws.current = sock;
      sock.onopen = () => setConnected(true);
      sock.onclose = () => { setConnected(false); setTimeout(connect, 2000); };
      sock.onerror = () => sock.close();
      sock.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.topic === 'debug/response') addLog(msg.payload?.response ?? '', '#0f0');
          // bin-card retrieves started elsewhere show up dimmed, for context
          else if (msg.topic === 'machine/crane/response') addLog(msg.payload?.response ?? '', '#777');
          else if (msg.barcode) addLog(`bin ${msg.barcode} -> ${msg.status}`, '#777');
        } catch {}
      };
    }
    connect();
    return () => { alive = false; ws.current?.close(); };
  }, [addLog]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  const send = useCallback((command) => {
    if (!command || ws.current?.readyState !== WebSocket.OPEN) return;
    addLog('> ' + command, '#e8b339');
    ws.current.send(JSON.stringify({ topic: 'debug/command', payload: { command } }));
  }, [addLog]);

  return (
    <div style={S.page}>
      <div style={S.head}>
        <span style={S.dot(connected)} />Debug Shell
      </div>
      <div style={S.groups}>
        {GROUPS.map(g => (
          <div key={g.label} style={S.group}>
            <span style={S.gname}>{g.label}</span>
            {g.cmds.map(([label, cmd]) => (
              <button key={cmd} style={cmd === 's' ? { ...S.btn, ...S.stop } : S.btn}
                      onClick={() => send(cmd)}>{label}</button>
            ))}
          </div>
        ))}
      </div>
      <div ref={logRef} style={S.log}>
        {log.map(l => <div key={l.key} style={{ color: l.color }}>{l.text}</div>)}
      </div>
      <div style={S.row}>
        <input style={S.input} value={input} placeholder="shell command or raw gcode  (m 1 0, save 2 3, dist, laser D, $$ ...)"
               onChange={e => setInput(e.target.value)}
               onKeyDown={e => { if (e.key === 'Enter') { send(input.trim()); setInput(''); } }} />
        <button style={S.send} onClick={() => { send(input.trim()); setInput(''); }}>Send</button>
      </div>
    </div>
  );
}
