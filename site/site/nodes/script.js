const $ = (selector) => document.querySelector(selector);
const REFRESH = 5000; // ms between list updates

let token;
try { token = sessionStorage.getItem('sinference.session'); } catch { /* shown as signed out */ }


/*******
 * API *
 *******/

function message(text, error = false) {
  $('#message').textContent = text;
  $('#message').dataset.error = String(error);
}

function showSignedOut() {
  $('#list').hidden = $('#empty').hidden = true;
  $('#signed-out').hidden = false;
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

  if (response.status === 401) showSignedOut();
  if (!response.ok) throw new Error(result.message || 'Request failed. Please try again.');
  return result;
}


/********
 * List *
 ********/

const gigabytes = (mb) => (mb / 1024).toFixed(1);

/* ready = holding its slice, online = connected, offline = gone */
function status(node) {
  if (node.status === 'ready') return 'Ready';
  if (node.status === 'online') return 'Connected';

  const minutes = Math.floor((Date.now() / 1000 - node.last_seen) / 60);
  if (minutes < 1) return 'Offline · just now';
  if (minutes < 60) return `Offline · ${minutes} min ago`;
  return `Offline · ${new Date(node.last_seen * 1000).toLocaleString()}`;
}

function render({ nodes, models }) {
  $('#signed-out').hidden = true;
  $('#empty').hidden = nodes.length > 0;
  $('#list').hidden = nodes.length === 0;

  const names = Object.fromEntries(models.map((model) => [model.id, model.name]));

  $('#rows').replaceChildren(...nodes.map((node) => {
    const row = $('#row').content.firstElementChild.cloneNode(true);
    row.dataset.status = node.status;

    row.querySelector('.name').textContent = node.name;
    row.querySelector('.ip').textContent = node.ip;
    row.querySelector('.vram').textContent = node.vram_free_mb === null
      ? 'No GPU detected'
      : `${gigabytes(node.vram_free_mb)} of ${gigabytes(node.vram_total_mb)} GB`;
    row.querySelector('.seen').textContent = status(node);

    /* model + a link to change it */
    row.querySelector('.model').textContent = names[node.model] ?? '';
    const choose = row.querySelector('.choose');
    choose.href = `/nodes/model/?node=${encodeURIComponent(node.id)}`;
    choose.textContent = node.model ? 'Change' : 'Choose a model';
    return row;
  }));
}

async function load() {
  const list = await api('/nodes/list');
  render(list);
  renderWallet(list).catch((error) => walletMessage(`Couldn't read your stake (${error.message}).`, true));
}

async function refresh() {
  try {
    await load();
    message('');
  } catch (error) {
    if (!$('#signed-out').hidden) return; // stop once signed out
    message(error.message, true);
  }
  setTimeout(refresh, REFRESH);
}



/**********
 * Wallet *
 **********/

/* the account's wallet: its nodes stake from it (as the coordinator, which keeps its key) + get paid to it */
function walletMessage(text, error = false) {
  $('#wallet-message').textContent = text;
  $('#wallet-message').dataset.error = String(error);
}

async function rpc(url, method, ...params) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
  });
  const { result, error } = await response.json();
  if (error) throw new Error(error.message);
  return result;
}

/* SOL in the stake above its rent (collateral + earnings not released yet), off the chain */
async function staked(url, address) {
  const { value } = await rpc(url, 'getAccountInfo', address, { encoding: 'base64', commitment: 'confirmed' });
  if (!value) return 'Nothing yet';
  const length = atob(value.data[0]).length;
  if (atob(value.data[0]).charCodeAt(56)) return 'Slashed after a failed audit';  // banned
  const rent = await rpc(url, 'getMinimumBalanceForRentExemption', length);
  return `${((value.lamports - rent) / 1e9).toLocaleString(undefined, { maximumFractionDigits: 6 })} SOL`;
}

async function renderWallet({ wallet, stake, solana }) {
  $('#wallet-card').hidden = !solana;
  if (!solana) return;

  $('#wallet-need').textContent = `Your nodes stake $${solana.min_stake_usd} of SOL each from this wallet while they `
    + 'serve, and get paid to it. It all goes back to the wallet once audits are past.';
  $('#wallet-figures').hidden = !wallet;
  $('#wallet-form').hidden = Boolean(wallet) && $('#replace').dataset.open !== 'true';
  $('#replace').hidden = !wallet;
  if (!wallet) return;

  $('#wallet').textContent = wallet;
  $('#staked').textContent = await staked(solana.rpc, stake);
}

$('#replace').addEventListener('click', () => {
  $('#replace').dataset.open = 'true';
  $('#wallet-form').hidden = false;
});

$('#wallet-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const { address } = await api('/nodes/wallet', { key: $('#key').value.trim() });
    $('#key').value = '';
    $('#replace').dataset.open = 'false';
    walletMessage(`Wallet ${address} added.`);
    await load();
  } catch (error) {
    walletMessage(error.message, true);
  }
});

if (token) refresh();
else showSignedOut();
