// Shared MapLibre setup for every map in the app (customer 3D map and the operations dashboard).
import { setWorkerUrl } from 'maplibre-gl';
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import 'maplibre-gl/dist/maplibre-gl.css';

// MapLibre 6 loads its tile worker from a separate ES module; let Vite bundle it so the URL is valid in dev and build.
setWorkerUrl(maplibreWorkerUrl);

export const BASEMAPS = {
  light: 'https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',
  dark: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
};
export const BASEMAP = BASEMAPS.light;
