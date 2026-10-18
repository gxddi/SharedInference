// Generate a standalone SVG using the site's existing geographic mask.
// Run from the repository root:
// magick site/site/assets/earth/earth-mask.png -resize 720x360! -depth 8 rgb:- | node scripts/misc/generate-globe.cjs
// Requires ImageMagick's `convert` command; the SVG has no runtime dependencies.
const { readFileSync, writeFileSync } = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '../..');
const width = 720;
const height = 360;
const pixels = readFileSync(0);
if (pixels.length !== width * height * 3) {
  throw new Error('Expected a 720×360 RGB mask on standard input; see the command above.');
}
const radians = Math.PI / 180;
const radius = 1850;
const center = [1900, 1740];
const longitude = -45 * radians;
const latitude = 24 * radians;

function project(lon, lat) {
  const phi = lat * radians;
  const delta = lon * radians - longitude;
  return [
    center[0] + radius * Math.cos(phi) * Math.sin(delta),
    center[1] - radius * (Math.cos(latitude) * Math.sin(phi)
      - Math.sin(latitude) * Math.cos(phi) * Math.cos(delta)),
    Math.sin(latitude) * Math.sin(phi)
      + Math.cos(latitude) * Math.cos(phi) * Math.cos(delta),
  ];
}
const fixed = n => n.toFixed(2);

let land = '';
let ice = '';
// Sample roughly equally spaced points over the sphere, avoiding polar crowding.
for (let lat = -85; lat <= 85; lat += 0.8) {
  const step = 0.8 / Math.cos(lat * radians);
  for (let lon = -180; lon < 180; lon += step) {
    const [x, y, depth] = project(lon, lat);
    if (depth <= 0.025) continue;
    const px = Math.min(width - 1, Math.floor((lon + 180) / 360 * width));
    const py = Math.min(height - 1, Math.floor((90 - lat) / 180 * height));
    const index = (py * width + px) * 3;
    if (pixels[index] < 100 && pixels[index + 2] < 100) continue;
    const dot = `<circle cx="${fixed(x)}" cy="${fixed(y)}" r="${fixed((1.3 + depth * 1.35) * radius / 775)}" opacity="${fixed(0.25 + depth * 0.75)}"/>`;
    if (pixels[index + 2] > 127) ice += dot;
    else land += dot;
  }
}

let grid = '';
function line(points) {
  let drawing = false;
  let d = '';
  for (const [lon, lat] of points) {
    const [x, y, depth] = project(lon, lat);
    if (depth < 0) { drawing = false; continue; }
    d += `${drawing ? 'L' : 'M'}${fixed(x)},${fixed(y)}`;
    drawing = true;
  }
  return `<path d="${d}"/>`;
}
for (let lon = -180; lon < 180; lon += 20) {
  grid += line(Array.from({ length: 181 }, (_, i) => [lon, i - 90]));
}
for (let lat = -60; lat <= 60; lat += 20) {
  grid += line(Array.from({ length: 361 }, (_, i) => [i - 180, lat]));
}

// Coordinates are approximate campus/city centers. Each pin tip sits directly
// on its geographic anchor; nearby pins can overlap.
const places = [
  { id: 'brampton', name: 'Brampton', lon: -79.7624, lat: 43.7315, labelSide: -1 },
  { id: 'utsc', name: 'UTSC', lon: -79.1864, lat: 43.7841, labelSide: 1 },
  { id: 'new-york', name: 'New York', lon: -74.006, lat: 40.7128, labelSide: 1 },
];
const pins = places.map(p => {
  const [x, y] = project(p.lon, p.lat);
  const labelX = x + p.labelSide * 24;
  const anchor = p.labelSide < 0 ? 'end' : 'start';
  return `<g id="${p.id}" aria-label="${p.name}">
    <title>${p.name}: ${p.lat}° N, ${-p.lon}° W</title>
    <g transform="translate(${fixed(x)},${fixed(y)})" fill="#f0eee8">
      <path d="M0,0 C-4,-8 -15,-17 -15,-28 A15,15 0 1 1 15,-28 C15,-17 4,-8 0,0Z" stroke="#11151f" stroke-width="2"/>
      <circle cy="-28" r="4.5" fill="#11151f"/>
    </g>
    <text x="${fixed(labelX)}" y="${fixed(y - 20)}" text-anchor="${anchor}" fill="#f0eee8" font-size="24" stroke="#11151f" stroke-width="3" paint-order="stroke">${p.name}</text>
  </g>`;
}).join('\n');

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="1680" viewBox="0 0 1800 1680" overflow="hidden" role="img" aria-labelledby="title description">
  <title id="title">Globe with pins at UTSC, Brampton, and New York City</title>
  <desc id="description">A cropped view of the upper-left portion of a large teal dotted globe, with pins directly at three geographic locations in North America. UTSC means University of Toronto Scarborough.</desc>
  <!-- Geographic mask source: see the adjacent README.md. Orthographic projection: https://d3js.org/d3-geo/azimuthal -->
  <defs>
    <radialGradient id="ocean" cx="32%" cy="25%" r="83%">
      <stop stop-color="#1d3540"/><stop offset=".6" stop-color="#111e29"/><stop offset="1" stop-color="#090e18"/>
    </radialGradient>
    <radialGradient id="shade" cx="28%" cy="24%" r="85%">
      <stop offset=".35" stop-color="#050910" stop-opacity="0"/><stop offset="1" stop-color="#050910" stop-opacity=".78"/>
    </radialGradient>
    <radialGradient id="halo">
      <stop offset=".89" stop-color="#5aa5a5" stop-opacity="0"/>
      <stop offset=".94" stop-color="#5aa5a5" stop-opacity=".13"/>
      <stop offset="1" stop-color="#5aa5a5" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <circle cx="${center[0]}" cy="${center[1]}" r="${fixed(radius * 724 / 675)}" fill="url(#halo)"/>
  <circle cx="${center[0]}" cy="${center[1]}" r="${radius}" fill="url(#ocean)" stroke="#5aa5a5" stroke-opacity=".35" stroke-width="2"/>
  <g fill="none" stroke="#8fc5cd" stroke-opacity=".12" stroke-width="1">${grid}</g>
  <g fill="#7fd4c1">${land}</g>
  <g fill="#cfe3ef">${ice}</g>
  <circle cx="${center[0]}" cy="${center[1]}" r="${radius - 1}" fill="url(#shade)"/>
  <g font-family="'DejaVu Sans Mono', 'Courier New', monospace">${pins}</g>
</svg>\n`;
const output = path.join(root, 'site/site/assets/earth/globe.svg');
writeFileSync(output, svg);
console.log(output);
