export default function Modal({ children, onClose }) {
  return (
    <div style={{
      position: 'fixed',
      top: 0, left: 0, right: 0, bottom: 0,
      backgroundColor: 'rgba(4, 6, 9, 0.72)',
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'center',
      zIndex: 1000,
    }}>
      <div style={{
        background: 'var(--panel)',
        padding: '20px 24px 24px',
        borderRadius: 8,
        minWidth: 300,
        maxWidth: '90vw',
        boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
        position: 'relative',
        border: '1px solid var(--line)',
        borderTop: '3px solid var(--accent)',
      }}>
        <button
          onClick={onClose}
          style={{
            position: 'absolute',
            top: 8,
            right: 10,
            border: 'none',
            background: 'transparent',
            fontSize: '1.1rem',
            cursor: 'pointer',
            color: 'var(--text-faint)',
            lineHeight: 1,
          }}
        >
          ×
        </button>
        {children}
      </div>
    </div>
  );
}
