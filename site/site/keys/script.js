const $ = (selector) => document.querySelector(selector);

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
  $('#create').hidden = $('#list').hidden = true;
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
  catch { throw new Error('The account service is unavailable. Try again shortly.'); }

  if (response.status === 401) showSignedOut();
  if (!response.ok) throw new Error(result.message || 'Request failed. Please try again.');
  return result;
}


/********
 * Keys *
 ********/

const date = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString() : 'Never';

function render({ keys }) {
  $('#create').hidden = false;
  $('#list').hidden = keys.length === 0;

  $('#rows').replaceChildren(...keys.map((key) => {
    const row = $('#row').content.firstElementChild.cloneNode(true);
    row.querySelector('.name').textContent = key.name;
    row.querySelector('.hint-cell').textContent = key.hint;
    row.querySelector('.created').textContent = date(key.created_at);
    row.querySelector('.used').textContent = date(key.last_used);

    row.querySelector('.revoke').addEventListener('click', async () => {
      if (!confirm(`Revoke "${key.name}"? Anything using it stops working.`)) return;
      try {
        await api('/auth/keys/revoke', { id: key.id });
        await refresh();
        message('Key revoked.');
      } catch (error) { message(error.message, true); }
    });
    return row;
  }));
}

async function refresh() {
  render(await api('/auth/keys'));
}

/* new key: shown once */
$('#create').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#create-button').disabled = true;
  try {
    const { key } = await api('/auth/keys', { name: $('#name').value.trim() });
    $('#new-key').textContent = key;
    $('#new').hidden = false;
    $('#name').value = '';
    await refresh();
    message('');
  } catch (error) {
    message(error.message, true);
  } finally {
    $('#create-button').disabled = false;
  }
});

$('#copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('#new-key').textContent);
    message('Key copied.');
  } catch {
    message('Select and copy the key above.');
  }
});

$('#base-url').textContent = location.origin + '/api/v1';
if (token) refresh().catch((error) => { if ($('#signed-out').hidden) message(error.message, true); });
else showSignedOut();
