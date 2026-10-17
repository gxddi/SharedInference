const $ = (selector) => document.querySelector(selector);
const nodeId = new URLSearchParams(location.search).get('node');

let token;
try { token = sessionStorage.getItem('sinference.session'); } catch { /* shown as signed out */ }

let node;


/*******
 * API *
 *******/

function message(text, error = false) {
  $('#message').textContent = text;
  $('#message').dataset.error = String(error);
}

async function api(path, data) {
  const response = await fetch('/api' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: 'no-store',
  });

  let result;
  try { result = await response.json(); }
  catch { throw new Error('The node service is unavailable. Try again shortly.'); }

  if (!response.ok) throw new Error(result.message || 'Request failed. Please try again.');
  return result;
}


/********
 * Pick *
 ********/

const gigabytes = (mb) => (mb / 1024).toFixed(1);
const STATUS = { ready: 'Ready', online: 'Connected', offline: 'Offline' };

function showNode() {
  $('#node').hidden = false;
  $('#node').dataset.status = node.status;
  $('#node-name').textContent = node.name;
  $('#node-state').textContent = STATUS[node.status];
  $('#node-memory').textContent = node.vram_total_mb === null
    ? 'No GPU detected. Only computers with an NVIDIA GPU, or an Intel Arc GPU with Resizable BAR on, can serve models.'
    : `${gigabytes(node.vram_free_mb)} GB of ${gigabytes(node.vram_total_mb)} GB VRAM available. `
      + 'Models bigger than this are split across nodes.';
}

function showModels(models) {
  if (node.vram_total_mb === null) return; // nothing to serve with

  /* whole model on this GPU, or just a slice of it */
  const fits = (model) => model.memory_gb * 1024 <= node.vram_total_mb;

  /* current model, else the biggest one that fits whole */
  const chosen = node.model ?? models.filter(fits).at(-1)?.id;

  $('#models').append(...models.map((model) => {
    const option = $('#option').content.firstElementChild.cloneNode(true);
    const input = option.querySelector('input');
    input.value = model.id;
    input.checked = model.id === chosen;

    option.querySelector('strong').textContent = model.name;
    option.querySelector('small').textContent = `About ${model.memory_gb} GB in all`;
    option.querySelector('.fit').textContent = fits(model) ? 'Fits whole' : 'Serves a slice';
    option.classList.toggle('too-big', !fits(model));
    return option;
  }));
  $('#pick').hidden = false;
}

$('#pick').addEventListener('submit', async (event) => {
  event.preventDefault();
  const model = new FormData(event.target).get('model');
  $('#use').disabled = true;
  message('Saving…');

  try {
    await api('/nodes/model', { node_id: node.id, model });
    node.model = model;
    message(node.status === 'offline'
      ? `Saved. ${node.name} gets it next time it connects.`
      : `Sent to ${node.name}. Your terminal shows its slice loading now.`);
  } catch (error) { message(error.message, true); }
  finally { $('#use').disabled = false; }
});


/********
 * Load *
 ********/

async function load() {
  if (!token) {
    $('#node-memory').innerHTML = '<a href="/login/">Sign in</a> to choose a model for your node.';
    return;
  }

  try {
    const { nodes, models } = await api('/nodes/list');
    node = nodes.find((candidate) => candidate.id === nodeId);
    if (!node) {
      $('#node-memory').textContent = 'This node isn’t on your account. Open the link from your terminal again.';
      return;
    }
    showNode();
    showModels(models);
  } catch (error) {
    $('#node-memory').textContent = '';
    message(error.message, true);
  }
}

load();
