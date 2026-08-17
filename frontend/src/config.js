// Default: same origin as the page, so one build works from any device.
// For CRA dev mode (npm start) point these at the backend explicitly, e.g.
// REACT_APP_API=http://localhost:3001 REACT_APP_WS_URL=ws://localhost:3001 npm start
export const BASE_URL = process.env.REACT_APP_API || '';
export const WS_URL = process.env.REACT_APP_WS_URL || `ws://${window.location.host}/ws`;
