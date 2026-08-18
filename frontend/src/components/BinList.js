import { useEffect, useState, useRef } from 'react';
import Icon from '@mdi/react';
import {
  mdiPlus,
  mdiPencilOutline,
  mdiTrashCanOutline,
} from '@mdi/js';
import { DragDropContext, Droppable, Draggable } from '@hello-pangea/dnd';
import Modal from './Modal';
import { BASE_URL, WS_URL } from '../config';

function getBinBorderColor(bin) {
  if (bin.status === 'out') return 'var(--red)';
  if (bin.status === 'out-pending') return 'var(--red)';
  if (bin.status === 'in-pending') return 'var(--green)';
  return 'var(--line)';
}

function getBinStatus(bin) {
  if (bin.status === 'in') return { label: 'IN STOCK', led: 'led-green', color: 'var(--green)' };
  if (bin.status === 'out') return { label: 'OUT', led: 'led-red', color: 'var(--red)' };
  if (bin.status === 'in-pending') return { label: 'STORING…', led: 'led-green', color: 'var(--green)' };
  return { label: 'RETRIEVING…', led: 'led-red', color: 'var(--red)' };
}

function getBinAnimationClass(bin) {
  if (bin.status === 'out-pending') return 'flash-red';
  if (bin.status === 'in-pending') return 'flash-green';
  return '';
}

function groupBins(bins) {
  const groups = {};
  for (const bin of bins) {
    const sub = bin.subcategory?.trim() || 'Uncategorized';
    if (!groups[sub]) groups[sub] = [];
    groups[sub].push(bin);
  }
  return groups;
}

async function putCategory(category, bins) {
  return fetch(`${BASE_URL}/category/${encodeURIComponent(category)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(bins),
  });
}

const inputStyle = {
  width: '100%',
  marginBottom: 10,
  padding: '7px 10px',
  border: '1px solid var(--line)',
  background: 'var(--bg)',
  color: 'var(--text)',
  fontSize: 13,
  borderRadius: 8,
  outline: 'none',
};

export default function BinList({ category, categoryList, parentName, selectedSubcategory, onBinsChanged }) {
  const [bins, setBins] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [editIndex, setEditIndex] = useState(null);
  const [newBin, setNewBin] = useState({ name: '', barcode: '', status: 'in', subcategory: '', request: 'no' });
  // Barcodes with a retrieve request sent but not yet acknowledged by the vehicle
  const [requested, setRequested] = useState(new Set());
  const requestTimers = useRef({});
  const clientRef = useRef(null);

  const clearRequested = (barcode) => {
    setRequested(prev => {
      if (!prev.has(barcode)) return prev;
      const next = new Set(prev);
      next.delete(barcode);
      return next;
    });
    clearTimeout(requestTimers.current[barcode]);
    delete requestTimers.current[barcode];
  };

  // Single category. Guard non-array responses (e.g. a 400 {error} for a stale
  // category name remembered in localStorage) - they must never reach setBins.
  const reloadRef = useRef(null);
  useEffect(() => {
    if (!category) { reloadRef.current = null; return; }
    const load = () => fetch(`${BASE_URL}/category/${encodeURIComponent(category)}`)
      .then(res => res.json())
      .then(data => setBins(Array.isArray(data) ? data : []))
      .catch(() => setBins([]));
    reloadRef.current = load;   // so the socket can re-read after a new bin
    load();
  }, [category]);

  // Parent group — fetch all categories and merge
  useEffect(() => {
    if (!categoryList?.length) return;
    Promise.all(categoryList.map(cat =>
      fetch(`${BASE_URL}/category/${encodeURIComponent(cat)}`)
        .then(res => res.json())
        .then(data => (Array.isArray(data) ? data : []))
        .catch(() => [])
    )).then(results => setBins(results.flat()));
  }, [categoryList]);

  // WebSocket connection is category-agnostic — connect once at mount
  useEffect(() => {
    const ws = new WebSocket(WS_URL);
    ws.onopen = () => console.log('[WS] connected to backend');
    ws.onerror = (e) => console.error('[WS] connection error', e);
    ws.onclose = () => console.warn('[WS] disconnected');
    ws.onmessage = (event) => {
      try {
        const { barcode, status } = JSON.parse(event.data);
        if (!barcode) return;            // machine progress lines, not bin state
        clearRequested(barcode); // vehicle acknowledged
        setBins(prev => {
          // A store can mint a brand new bin (scanned label nobody had entered).
          // It is not in our list, so re-read the category to pick it up.
          if (!prev.some(b => b.barcode === barcode)) {
            reloadRef.current?.();
            return prev;
          }
          return prev.map(bin =>
            bin.barcode === barcode ? { ...bin, status } : bin);
        });
      } catch (e) { console.error('[WS] message parse error', e); }
    };
    clientRef.current = ws;
    const timers = requestTimers.current;
    return () => {
      ws.close();
      Object.values(timers).forEach(clearTimeout);
    };
  }, []);

  const handleBinClick = (barcode) => {
    if (requested.has(barcode)) return; // already awaiting acknowledgement
    if (!window.confirm('Retrieve bin?')) return;
    const ws = clientRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      const msg = JSON.stringify({ topic: 'bins/command', payload: { barcode, type: 'retrieve' } });
      console.log('[WS] sending', msg);
      ws.send(msg);
      // Status itself is not updated optimistically (see MQTT_MESSAGES.md) — we only
      // mark the request as awaiting acknowledgement, cleared by the next bins/update
      setRequested(prev => new Set(prev).add(barcode));
      clearTimeout(requestTimers.current[barcode]);
      requestTimers.current[barcode] = setTimeout(() => clearRequested(barcode), 15000);
    } else {
      console.error('[WS] not open, readyState:', ws?.readyState);
    }
  };

  const handleDragEnd = async ({ source, destination, draggableId }) => {
    if (!destination) return;
    const fromSubcat = source.droppableId;
    const toSubcat = destination.droppableId;
    const grouped = groupBins(bins);
    const draggedBin = grouped[fromSubcat].find(bin => bin.barcode === draggableId);
    if (!draggedBin) return;
    grouped[fromSubcat] = grouped[fromSubcat].filter(b => b.barcode !== draggableId);
    grouped[toSubcat] = [...(grouped[toSubcat] || []), { ...draggedBin, subcategory: toSubcat }];
    const newList = Object.values(grouped).flat();
    setBins(newList);
    await putCategory(category, newList);
    onBinsChanged?.();
  };

  const handleAddOrUpdateBin = async () => {
    if (!newBin.name || !newBin.barcode) return;
    // Writes go to ONE category file. With a parent group selected there is no
    // single target, and posting to `/category/undefined` looked like it worked
    // while the bin landed in a stray file and vanished on the next refresh.
    if (!category) {
      window.alert('Pick a single category before adding a bin.');
      return;
    }
    let res;
    if (editIndex !== null) {
      const updatedBins = [...bins];
      updatedBins[editIndex] = newBin;
      res = await putCategory(category, updatedBins);
    } else {
      res = await fetch(`${BASE_URL}/category/${encodeURIComponent(category)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newBin),
      });
    }
    if (!res?.ok) {
      window.alert('Could not save the bin - it was not written to disk.');
      return;
    }
    // Re-read from the server instead of patching local state, so what is on
    // screen is what actually persisted - no silent divergence until a refresh.
    const saved = await fetch(`${BASE_URL}/category/${encodeURIComponent(category)}`)
      .then(r => r.json()).catch(() => null);
    setBins(Array.isArray(saved) ? saved : bins);
    setShowForm(false);
    setEditIndex(null);
    setNewBin({ name: '', barcode: '', status: 'in', subcategory: '', request: 'no' });
    onBinsChanged?.();
  };

  const handleEditBin = (index) => {
    setEditIndex(index);
    setNewBin(bins[index]);
    setShowForm(true);
  };

  const handleDeleteBin = async (index) => {
    if (!window.confirm('Delete this bin?')) return;
    const updatedBins = [...bins];
    updatedBins.splice(index, 1);
    await putCategory(category, updatedBins);
    setBins(updatedBins);
    onBinsChanged?.();
  };

  const grouped = groupBins(bins);
  const visibleGroups = selectedSubcategory
    ? (grouped[selectedSubcategory] ? { [selectedSubcategory]: grouped[selectedSubcategory] } : {})
    : grouped;

  return (
    <div style={{ flex: 1, overflowY: 'auto', backgroundColor: 'var(--bg)' }}>
      {/* Content header bar */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '10px 18px',
        borderBottom: '1px solid var(--line)',
        backgroundColor: 'var(--panel)',
      }}>
        <span style={{
          fontFamily: 'var(--font-display)',
          fontWeight: 700,
          fontSize: 18,
          letterSpacing: '1.5px',
          textTransform: 'uppercase',
          color: 'var(--text)',
        }}>
          {parentName ? parentName : category ? category : 'Select a category'}
          {selectedSubcategory && <span style={{ color: 'var(--text-faint)', fontWeight: 600 }}> / {selectedSubcategory}</span>}
        </span>
        {category && !parentName && (
          <button
            onClick={() => { setShowForm(true); setEditIndex(null); setNewBin({ name: '', barcode: '', status: 'in', subcategory: '' }); }}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              backgroundColor: 'var(--accent)',
              color: '#ffffff',
              border: 'none',
              padding: '6px 14px',
              fontFamily: 'var(--font-display)',
              fontWeight: 700,
              fontSize: 14,
              textTransform: 'uppercase',
              cursor: 'pointer',
              letterSpacing: '1px',
              borderRadius: 8,
            }}
          >
            <Icon path={mdiPlus} size={0.7} color="#ffffff" /> Add Bin
          </button>
        )}
      </div>

      <div style={{ padding: 16 }}>
        {showForm && (
          <Modal onClose={() => { setShowForm(false); setEditIndex(null); }}>
            <h3 style={{ margin: '0 0 14px', fontFamily: 'var(--font-display)', fontSize: 17, letterSpacing: '1px', textTransform: 'uppercase', color: 'var(--text)' }}>
              {editIndex !== null ? 'Edit Bin' : 'Add Bin'}
            </h3>
            <input placeholder="Name" value={newBin.name} onChange={e => setNewBin({ ...newBin, name: e.target.value })} style={inputStyle} />
            <input placeholder="Barcode" value={newBin.barcode} onChange={e => setNewBin({ ...newBin, barcode: e.target.value })} style={inputStyle} />
            <input placeholder="Subcategory" value={newBin.subcategory} onChange={e => setNewBin({ ...newBin, subcategory: e.target.value })} style={{ ...inputStyle, marginBottom: 16 }} />
            <button
              onClick={handleAddOrUpdateBin}
              style={{ backgroundColor: 'var(--accent)', color: '#ffffff', border: 'none', padding: '8px 20px', fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 14, letterSpacing: '1px', textTransform: 'uppercase', cursor: 'pointer', borderRadius: 8 }}
            >
              {editIndex !== null ? 'Save' : 'Add'}
            </button>
          </Modal>
        )}

        <DragDropContext onDragEnd={handleDragEnd}>
          {Object.entries(visibleGroups).map(([subcat, list]) => (
            <div key={subcat} style={{ marginBottom: 28 }}>
              <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                fontFamily: 'var(--font-mono)',
                fontSize: 11,
                fontWeight: 600,
                textTransform: 'uppercase',
                letterSpacing: '2px',
                color: 'var(--text-dim)',
                marginBottom: 10,
                paddingBottom: 5,
                borderBottom: '1px solid var(--line)',
              }}>
                <span style={{ width: 10, height: 10, background: 'var(--accent)', flexShrink: 0 }} />
                {subcat}
                <span style={{ marginLeft: 'auto', color: 'var(--text-faint)', letterSpacing: '1px' }}>{list.length.toString().padStart(2, '0')}</span>
              </div>
              <Droppable droppableId={subcat} direction="horizontal">
                {(provided) => (
                  <div
                    ref={provided.innerRef}
                    {...provided.droppableProps}
                    style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}
                  >
                    {list.map((bin, index) => (
                      <Draggable key={bin.barcode} draggableId={bin.barcode} index={index}>
                        {(provided) => (
                          <div
                            ref={provided.innerRef}
                            {...provided.draggableProps}
                            {...provided.dragHandleProps}
                            className={requested.has(bin.barcode) ? 'bin-requested' : getBinAnimationClass(bin)}
                            onClick={() => handleBinClick(bin.barcode)}
                            style={{
                              width: 152,
                              padding: '10px 12px 9px',
                              borderRadius: 8,
                              backgroundColor: 'var(--raised)',
                              border: requested.has(bin.barcode)
                                ? '1px dashed var(--accent)'
                                : `1px solid ${getBinBorderColor(bin)}`,
                              borderTop: requested.has(bin.barcode)
                                ? '3px dashed var(--accent)'
                                : `3px solid ${getBinBorderColor(bin)}`,
                              fontSize: 12,
                              boxShadow: '0 2px 6px rgba(0,0,0,0.35)',
                              position: 'relative',
                              userSelect: 'none',
                              cursor: 'pointer',
                              ...provided.draggableProps.style,
                            }}
                          >
                            <div style={{ position: 'absolute', top: 5, right: 4, display: 'flex' }}>
                              <button
                                onClick={e => { e.stopPropagation(); handleEditBin(bins.findIndex(b => b.barcode === bin.barcode)); }}
                                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px 3px', color: 'var(--text-faint)' }}
                                title="Edit"
                              >
                                <Icon path={mdiPencilOutline} size={0.6} />
                              </button>
                              <button
                                onClick={e => { e.stopPropagation(); handleDeleteBin(bins.findIndex(b => b.barcode === bin.barcode)); }}
                                style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px 3px', color: 'var(--red)' }}
                                title="Delete"
                              >
                                <Icon path={mdiTrashCanOutline} size={0.6} />
                              </button>
                            </div>

                            <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 3, paddingRight: 36, color: 'var(--text)' }}>
                              {bin.name}
                            </div>
                            <div style={{ color: 'var(--text-faint)', fontSize: 10, marginBottom: 8, fontFamily: 'var(--font-mono)', letterSpacing: '0.5px' }}>
                              {bin.barcode}
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                              {requested.has(bin.barcode) ? (
                                <>
                                  <span className="led led-accent" />
                                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 600, letterSpacing: '1px', color: 'var(--accent)' }}>
                                    REQUESTED…
                                  </span>
                                </>
                              ) : (
                                <>
                                  <span className={`led ${getBinStatus(bin).led}`} />
                                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 600, letterSpacing: '1px', color: getBinStatus(bin).color }}>
                                    {getBinStatus(bin).label}
                                  </span>
                                </>
                              )}
                            </div>
                          </div>
                        )}
                      </Draggable>
                    ))}
                    {provided.placeholder}
                  </div>
                )}
              </Droppable>
            </div>
          ))}
        </DragDropContext>
      </div>
    </div>
  );
}
