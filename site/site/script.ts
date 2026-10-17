import * as THREE from './vendor/three/three.module.js';

const earth = document.querySelector('.earth');
const canvas = earth?.querySelector('canvas');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');


/*********
 * Earth *
 *********/

const OCEAN = '#11151f';
const LAND = '#7fd4c1';
const ICE = '#cfe3ef';

/* dot-matrix texture from the land (R) / ice (B) mask */
function paint(mask) {
  const { width, height } = mask;
  const source = document.createElement('canvas');
  source.width = width;
  source.height = height;
  const sourceContext = source.getContext('2d', { willReadFrequently: true });
  sourceContext.drawImage(mask, 0, 0);
  const pixels = sourceContext.getImageData(0, 0, width, height).data;

  /* output at double resolution so dots stay round and crisp */
  const out = document.createElement('canvas');
  out.width = width * 2;
  out.height = height * 2;
  const context = out.getContext('2d');
  context.fillStyle = OCEAN;
  context.fillRect(0, 0, out.width, out.height);

  const cell = 5;
  for (let y = 0; y < height; y += cell) {
    for (let x = 0; x < width; x += cell) {
      const i = ((y + (cell >> 1)) * width + x + (cell >> 1)) * 4;
      if (pixels[i + 2] > 127) context.fillStyle = ICE;
      else if (pixels[i] > 127) context.fillStyle = LAND;
      else continue;
      context.beginPath();
      context.arc((x + cell / 2) * 2, (y + cell / 2) * 2, cell * .7, 0, 2 * Math.PI);
      context.fill();
    }
  }
  return out;
}

async function createEarth() {
  if (!canvas) return;

  /* texture */
  const mask = await new THREE.ImageLoader().loadAsync('/assets/earth/earth-mask.png');
  const texture = new THREE.CanvasTexture(paint(mask));
  texture.colorSpace = THREE.SRGBColorSpace;

  /* renderer */
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  } catch {
    texture.dispose();
    return;
  }
  renderer.setClearColor(0x000000, 0);
  texture.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());

  /* scene */
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1.0435, 1.0435, 1.0435, -1.0435, .1, 10);
  camera.position.z = 4;

  /* globe */
  const geometry = new THREE.SphereGeometry(1, 32, 24);
  const material = new THREE.MeshStandardMaterial({ map: texture, roughness: .95, metalness: 0 });
  const globe = new THREE.Mesh(geometry, material);
  globe.rotation.set(.12, 2.3, -.15);
  scene.add(globe);

  /* sunlight */
  scene.add(new THREE.AmbientLight(0xb7c8dc, .5));
  const sun = new THREE.DirectionalLight(0xffefda, 1.6);
  sun.position.set(-3, 3, 5);
  scene.add(sun);

  /* outline */
  const outline = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
    color: 0x5aa5a5,
    transparent: true,
    opacity: .3,
    side: THREE.BackSide
  }));
  outline.scale.setScalar(1.03);
  scene.add(outline);


  /*************
   * Rendering *
   *************/

  let previous = 0;
  let contextLost = false;

  const render = () => renderer.render(scene, camera);

  const animate = (time) => {
    const delta = previous ? Math.min((time - previous) / 1000, .05) : 0;
    previous = time;
    globe.rotation.y += delta * .01;
    render();
  };

  const resize = () => {
    const size = earth.clientWidth;
    const ratio = Math.min(window.devicePixelRatio || 1, 1.5, 1280 / size);
    renderer.setSize(Math.round(size * ratio), Math.round(size * ratio), false);
    if (!contextLost) render();
  };

  const updateMotion = () => {
    previous = 0;
    const paused = reducedMotion.matches || document.hidden || contextLost;
    renderer.setAnimationLoop(paused ? null : animate);
    if (!contextLost) render();
  };

  canvas.addEventListener('webglcontextlost', () => {
    contextLost = true;
    earth.classList.remove('earth--ready');
    updateMotion();
  });
  canvas.addEventListener('webglcontextrestored', () => {
    contextLost = false;
    resize();
    updateMotion();
    earth.classList.add('earth--ready');
  });

  new ResizeObserver(resize).observe(earth);
  reducedMotion.addEventListener('change', updateMotion);
  document.addEventListener('visibilitychange', updateMotion);

  resize();
  updateMotion();
  earth.classList.add('earth--ready');
}

createEarth().catch(() => {}); // the still Earth remains if loading fails
