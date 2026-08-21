import { useEffect, useState, useRef } from 'react';
import Icon from '@mdi/react';
import {
  mdiPlus,
  mdiPencilOutline,
  mdiTrashCanOutline,
} from '@mdi/js';
import Modal from './Modal';
import { BASE_URL, WS_URL } from '../config';

// Every bin wears its status. In stock used to fall through to a plain grey
// border, which meant the most common card carried no colour at all and the
// grid read as a wall of white against a grey page.
function getBinBorderColor(bin) {
  if (bin.status === 'missing') return 'var(--amber)';
  if (bin.status === 'out' || bin.status === 'out-pending') return 'var(--red)';
  return 'var(--green)';
}

// Faint wash of the same colour across the card face, so status is legible
// from across the room rather than only at the border.
function getBinTint(bin) {
  if (bin.status === 'missing') return 'var(--amber-soft)';
  if (bin.status === 'out' || bin.status === 'out-pending') return 'var(--red-soft)';
  return 'var(--green-soft)';
}

function getBinStatus(bin) {
  // Set when a slot turned out to hold something else. The rack's record said
  // this bin was there and it was not, so nothing knows where it is.
  if (bin.status === 'missing') return { label: 'MISSING', led: 'led-amber', color: 'var(--amber)' };
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

// Where a bin goes when it is added without picking a category. Same name the
// Pi's inventory.ensure() uses for a scanned label nobody has filed yet, so the
// two paths land in one place. POSTing a bin to a category file that does not
// exist creates it, so this needs no setup.
const UNCATEGORIZED = 'uncategorized';

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

export default function BinList({ category, allCategories, categoryList, parentName, selectedSubcategory, onBinsChanged }) {
  const [bins, setBins] = useState([]);
  const [query, setQuery] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editIndex, setEditIndex] = useState(null);
  // Which category a NEW bin is filed under. Separate from `category` (what is
  // on screen) so a bin can be added from anywhere - including a parent group,
  // or with nothing selected at all - without first navigating to a category.
  const [target, setTarget] = useState(UNCATEGORIZED);
  // A hand-added bin starts OUT. Typing a bin into the roster records that it
  // exists, not that the machine is holding it - only a store mission that
  // actually scans and shelves it can say that, and do_store flips it to 'in'
  // when it finishes. Defaulting to 'in' claimed stock the rack did not have.
  const [newBin, setNewBin] = useState({ name: '', barcode: '', status: 'out', subcategory: '' });
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

  // Parent group - fetch every category in it and merge. Each bin is tagged
  // with the category it came out of, because once they are merged the card is
  // the only place that can still say which one that was. Underscore-prefixed
  // and added client-side only: it is never written back (edits are blocked in
  // this view anyway, since there is no single file to rewrite).
  useEffect(() => {
    if (!categoryList?.length) return;
    Promise.all(categoryList.map(cat =>
      fetch(`${BASE_URL}/category/${encodeURIComponent(cat)}`)
        .then(res => res.json())
        .then(data => (Array.isArray(data) ? data : []).map(b => ({ ...b, _category: cat })))
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

  // Only a bin the rack is actually holding can be fetched. 'out' means it is
  // already off the machine, and the two -pending states mean a sequence is
  // mid-flight - asking for any of them would send the car after a slot that
  // is empty or about to change.
  const canRetrieve = (bin) => bin.status === 'in' && !requested.has(bin.barcode);

  const handleBinClick = (bin) => {
    if (!canRetrieve(bin)) return;
    const barcode = bin.barcode;
    if (!window.confirm(`Retrieve "${bin.name}"?`)) return;
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

  const handleAddOrUpdateBin = async () => {
    if (!newBin.name || !newBin.barcode) return;
    let res;
    if (editIndex !== null) {
      // An edit rewrites the whole file it came from, so it needs the single
      // category on screen. From a parent group the bins are merged out of
      // several files and there is no one file to write back to - PUTting to
      // `/category/undefined` would create a stray file and lose them.
      if (!category) {
        window.alert('Open a single category to edit a bin.');
        return;
      }
      const updatedBins = [...bins];
      updatedBins[editIndex] = category === UNCATEGORIZED
        ? { ...newBin, subcategory: '' }   // no subcategory layer in this one
        : newBin;
      res = await putCategory(category, updatedBins);
    } else {
      // Adds go wherever the form says, which is why they work from anywhere.
      // Nothing gets a subcategory in the unfiled bucket - it has no such layer.
      const payload = target === UNCATEGORIZED ? { ...newBin, subcategory: '' } : newBin;
      res = await fetch(`${BASE_URL}/category/${encodeURIComponent(target)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    }
    if (!res?.ok) {
      // Show what the server objected to - a duplicate barcode names the bin
      // and the category it clashes with, which a generic message would hide.
      const { error } = await res.json().catch(() => ({}));
      window.alert(error || 'Could not save the bin - it was not written to disk.');
      return;
    }
    // Re-read from the server instead of patching local state, so what is on
    // screen is what actually persisted - no silent divergence until a refresh.
    // Re-read the category ON SCREEN, which may not be the one just written to.
    if (category) {
      const saved = await fetch(`${BASE_URL}/category/${encodeURIComponent(category)}`)
        .then(r => r.json()).catch(() => null);
      setBins(Array.isArray(saved) ? saved : bins);
    }
    setShowForm(false);
    setEditIndex(null);
    setNewBin({ name: '', barcode: '', status: 'out', subcategory: '' });
    onBinsChanged?.();
  };

  const handleEditBin = (index) => {
    setEditIndex(index);
    setNewBin(bins[index]);
    setShowForm(true);
  };

  const handleDeleteBin = async (index) => {
    if (!category) {          // see handleAddOrUpdateBin - no single file to rewrite
      window.alert('Open a single category to delete a bin.');
      return;
    }
    if (!window.confirm('Delete this bin?')) return;
    const updatedBins = [...bins];
    updatedBins.splice(index, 1);
    await putCategory(category, updatedBins);
    setBins(updatedBins);
    onBinsChanged?.();
  };

  // The unfiled bucket has no subcategory layer - it is solely bins without a
  // home, so grouping them under a heading called "Uncategorized" inside a
  // category called "uncategorized" only added a row that said nothing.
  // Adding into the unfiled bucket offers no subcategory field, for the same
  // reason the view has no subcategory rows.
  const hideSubcat = editIndex === null
    ? target === UNCATEGORIZED           // adding: depends on where it is going
    : category === UNCATEGORIZED;        // editing: depends on where it already is
  // Search filters the bins BEFORE anything is grouped, so a subcategory or a
  // category with no matches produces no heading at all rather than an empty
  // one. It searches whatever the current view holds - a category, a
  // subcategory, a parent group, or all bins - rather than jumping scope.
  const q = query.trim().toLowerCase();
  const shown = q
    ? bins.filter(b =>
        (b.name || '').toLowerCase().includes(q) ||
        (b.barcode || '').toLowerCase().includes(q) ||
        (b.subcategory || '').toLowerCase().includes(q))
    : bins;

  const flat = category === UNCATEGORIZED;
  // In a parent group the bins come from several categories at once, so they
  // are split by category first and each gets its own heading.
  const byCategory = parentName
    ? shown.reduce((acc, b) => {
        const c = b._category || UNCATEGORIZED;
        (acc[c] = acc[c] || []).push(b);
        return acc;
      }, {})
    : null;
  const grouped = flat
    ? (shown.length ? { all: shown } : {})
    : groupBins(shown);
  const visibleGroups = (!flat && selectedSubcategory)
    ? (grouped[selectedSubcategory] ? { [selectedSubcategory]: grouped[selectedSubcategory] } : {})
    : grouped;

  // One subcategory section per group: heading, then the bin cards. Pulled
  // out because a parent group renders it once per category.
  const renderGroups = (groups) => (
    <>
        {Object.entries(groups).map(([subcat, list]) => (
          <div key={subcat} style={{ marginBottom: 28 }}>
            {!flat && <div style={{
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
            </div>}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {list.map((bin) => (
                <div
                  key={bin.barcode}
                  className={requested.has(bin.barcode) ? 'bin-requested' : getBinAnimationClass(bin)}
                  onClick={() => handleBinClick(bin)}
                  style={{
                    width: 152,
                    padding: '10px 12px 9px',
                    borderRadius: 8,
                    backgroundColor: getBinTint(bin),
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
                    cursor: canRetrieve(bin) ? 'pointer' : 'default',
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
              ))}
            </div>
          </div>
        ))}
    </>
  );

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
        <input
          placeholder="Search bins"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === 'Escape' && setQuery('')}
          style={{
            flex: 1,
            maxWidth: 260,
            marginLeft: 'auto',
            marginRight: 12,
            padding: '5px 9px',
            border: '1px solid var(--line)',
            borderRadius: 6,
            background: 'var(--raised)',
            color: 'var(--text)',
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
          }}
        />
        {/* Always available - a bin can be filed from anywhere, including a
          parent group or a fresh install with nothing selected. */}
        <button
          onClick={() => {
            setShowForm(true);
            setEditIndex(null);
            setTarget(category || UNCATEGORIZED);   // sensible default, still changeable
            setNewBin({ name: '', barcode: '', status: 'out', subcategory: '' });
          }}
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
      </div>

      <div style={{ padding: 16 }}>
        {showForm && (
          <Modal onClose={() => { setShowForm(false); setEditIndex(null); }}>
            <h3 style={{ margin: '0 0 14px', fontFamily: 'var(--font-display)', fontSize: 17, letterSpacing: '1px', textTransform: 'uppercase', color: 'var(--text)' }}>
              {editIndex !== null ? 'Edit Bin' : 'Add Bin'}
            </h3>
            {editIndex === null && (
              <select
                value={target}
                onChange={e => setTarget(e.target.value)}
                style={inputStyle}
              >
                {!(allCategories || []).includes(UNCATEGORIZED) && (
                  <option value={UNCATEGORIZED}>Uncategorized</option>
                )}
                {(allCategories || []).map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            )}
            <input placeholder="Name" value={newBin.name} onChange={e => setNewBin({ ...newBin, name: e.target.value })} style={inputStyle} />
            <input placeholder="Barcode" value={newBin.barcode} onChange={e => setNewBin({ ...newBin, barcode: e.target.value })}
                   style={hideSubcat ? { ...inputStyle, marginBottom: 16 } : inputStyle} />
            {!hideSubcat && (
              <input placeholder="Subcategory" value={newBin.subcategory} onChange={e => setNewBin({ ...newBin, subcategory: e.target.value })} style={{ ...inputStyle, marginBottom: 16 }} />
            )}
            <button
              onClick={handleAddOrUpdateBin}
              style={{ backgroundColor: 'var(--accent)', color: '#ffffff', border: 'none', padding: '8px 20px', fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 14, letterSpacing: '1px', textTransform: 'uppercase', cursor: 'pointer', borderRadius: 8 }}
            >
              {editIndex !== null ? 'Save' : 'Add'}
            </button>
          </Modal>
        )}

        <div>
          {q && shown.length === 0 && (
            <div style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              color: 'var(--text-faint)',
              padding: '8px 2px',
            }}>
              No bins match "{query.trim()}"
            </div>
          )}
          {byCategory
            ? Object.entries(byCategory).map(([cat, catBins]) => (
                <div key={cat} style={{ marginBottom: 8 }}>
                  {/* One bold heading per folder, matching the single-category
                      header - a parent group merges several categories, so
                      without this there is nothing saying where a bin lives. */}
                  <div style={{
                    fontFamily: 'var(--font-display)',
                    fontWeight: 700,
                    fontSize: 18,
                    letterSpacing: '1.5px',
                    textTransform: 'uppercase',
                    color: 'var(--text)',
                    marginBottom: 14,
                  }}>
                    {cat}
                  </div>
                  {renderGroups(groupBins(catBins))}
                </div>
              ))
            : renderGroups(visibleGroups)}
        </div>
      </div>
    </div>
  );
}
