import { useState, useEffect, useCallback, useRef } from 'react';
import { Routes, Route, NavLink } from 'react-router-dom';
import Sidebar from './components/Sidebar';
import BinList from './components/BinList';
import DebugPage from './components/DebugPage';
import { BASE_URL } from './config';
import './App.css';

function App() {
  const [categories, setCategories] = useState([]);
  const [selectedCategory, setSelectedCategory] = useState(() => localStorage.getItem('selectedCategory') || null);
  const [selectedSubcategory, setSelectedSubcategory] = useState(() => {
    try { return JSON.parse(localStorage.getItem('selectedSubcategory')) || null; } catch { return null; }
  });
  const [selectedParent, setSelectedParent] = useState(() => {
    try { return JSON.parse(localStorage.getItem('selectedParent')) || null; } catch { return null; }
  });
  const sidebarRef = useRef();

  const loadCategories = useCallback(() => {
    fetch(`${BASE_URL}/categories`)
      .then(res => res.json())
      .then(data => setCategories(Array.isArray(data) ? data : []))
      .catch(() => setCategories([]));
  }, []);

  useEffect(() => { loadCategories(); }, [loadCategories]);

  useEffect(() => {
    if (!categories.length) return;
    // a remembered category that no longer exists (stale localStorage) must be
    // dropped, or its fetches 400 forever
    if (selectedCategory && !categories.includes(selectedCategory)) {
      setSelectedCategory(categories[0]);
      setSelectedSubcategory(null);
    } else if (!selectedCategory && !selectedParent) {
      setSelectedCategory(categories[0]);
    }
  }, [categories, selectedCategory, selectedParent]);

  useEffect(() => {
    if (selectedCategory) localStorage.setItem('selectedCategory', selectedCategory);
    else localStorage.removeItem('selectedCategory');
  }, [selectedCategory]);

  useEffect(() => {
    if (selectedSubcategory) localStorage.setItem('selectedSubcategory', JSON.stringify(selectedSubcategory));
    else localStorage.removeItem('selectedSubcategory');
  }, [selectedSubcategory]);

  useEffect(() => {
    if (selectedParent) localStorage.setItem('selectedParent', JSON.stringify(selectedParent));
    else localStorage.removeItem('selectedParent');
  }, [selectedParent]);

  const handleSelectCategory = (cat) => {
    setSelectedCategory(cat);
    setSelectedParent(null);
    setSelectedSubcategory(null);
  };

  // Picking a subcategory sets BOTH pieces of state in one place. Doing it as
  // setSelectedSubcategory(x) followed by handleSelectCategory(cat) does not
  // work - that handler clears the subcategory, so the later write won and the
  // filter was wiped the instant it was set.
  const handleSelectSubcategory = (cat, subcat) => {
    setSelectedCategory(cat);
    setSelectedParent(null);
    setSelectedSubcategory({ cat, subcat });
  };

  const handleSelectParent = (parentName, parentCategories) => {
    setSelectedParent({ name: parentName, categories: parentCategories });
    setSelectedCategory(null);
    setSelectedSubcategory(null);
  };

  const handleBinsChanged = useCallback(() => {
    loadCategories();
    sidebarRef.current?.refreshExpandedBins();
  }, [loadCategories]);

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <h1>BIN</h1>
        <span className="tagline">Automated Storage // 4×8×3</span>
        <nav className="app-nav">
          <NavLink to="/" end className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>Bins</NavLink>
          <NavLink to="/debug" className={({ isActive }) => isActive ? 'nav-link active' : 'nav-link'}>Debug</NavLink>
        </nav>
      </div>
      <div className="app-body">
        <Routes>
          <Route path="/" element={
            <>
              <Sidebar
                ref={sidebarRef}
                categories={categories}
                selectedCategory={selectedCategory}
                selectedParent={selectedParent?.name}
                onSelect={handleSelectCategory}
                onSelectSubcategory={handleSelectSubcategory}
                onSelectParent={handleSelectParent}
                reloadCategories={loadCategories}
                selectedSubcategory={selectedSubcategory}
                setSelectedSubcategory={setSelectedSubcategory}
              />
              <BinList
                category={selectedCategory}
                allCategories={categories}
                categoryList={selectedParent?.categories}
                parentName={selectedParent?.name}
                selectedSubcategory={selectedSubcategory?.subcat}
                onBinsChanged={handleBinsChanged}
              />
            </>
          } />
          <Route path="/debug" element={<DebugPage />} />
        </Routes>
      </div>
    </div>
  );
}

export default App;
