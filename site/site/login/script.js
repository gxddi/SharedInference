const $ = (selector) => document.querySelector(selector);
const storageKey = 'sinference.session';
const code = new URLSearchParams(location.search).get('code'); // set when a terminal sent us here

let token;
try { token = sessionStorage.getItem(storageKey); } catch { /* in-memory sign-in still works */ }

let signup = false;   // sign in ↔ create account
let finished = false; // terminal already approved


/*******
 * API *
 *******/

function storeToken(value) {
  token = value;
  try {
    if (value) sessionStorage.setItem(storageKey, value);
    else sessionStorage.removeItem(storageKey);
  } catch { /* keep the session in memory when storage is unavailable */ }
}

function message(text, error = false) {
  $('#message').textContent = text;
  $('#message').dataset.error = String(error);
}

async function api(path, data) {
  const response = await fetch('/api' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: 'Bearer ' + token } : {}) },
    body: data === undefined ? undefined : JSON.stringify(data),
    cache: 'no-store',
  });

  let result;
  try { result = await response.json(); }
  catch { throw new Error('The account service is unavailable. Try again shortly.'); }

  if (!response.ok) {
    /* session's gone, back to the form */
    if (response.status === 401 && token) {
      storeToken(null);
      $('#auth-form').hidden = false;
      $('#signed-in').hidden = true;
      $('#device').hidden = true;
    }
    throw new Error(result.message || 'Request failed. Please try again.');
  }
  return result;
}


/***********
 * Account *
 ***********/

async function showAccount(username) {
  $('#auth-form').hidden = true;
  $('#signed-in').hidden = false;
  $('#account-username').textContent = username;
  $('#auth-title').textContent = code ? 'Connect your node.' : 'Your account.';
  $('#auth-intro').textContent = code ? 'Confirm the request from your terminal.' : 'You’re signed in to Shared Inference.';

  /* came from a terminal: show what's asking to connect */
  if (code && !finished) {
    const details = await api('/auth/device/details', { user_code: code });
    $('#device-name').textContent = details.device_name;
    $('#device-code').textContent = details.user_code;
    $('#device').hidden = false;
  }
}

$('#switch-auth').addEventListener('click', () => {
  signup = !signup;
  $('#auth-title').textContent = signup ? 'Create an account.' : 'Sign in.';
  $('#submit-auth').textContent = signup ? 'Create account' : 'Sign in';
  $('#switch-label').textContent = signup ? 'Already have an account?' : 'New here?';
  $('#switch-auth').textContent = signup ? 'Sign in' : 'Create an account';
  $('#password').autocomplete = signup ? 'new-password' : 'current-password';
  $('#password').minLength = signup ? 12 : 1;
  message('');
});

$('#auth-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#submit-auth').disabled = true;
  message(signup ? 'Creating your account…' : 'Signing in…');

  try {
    const result = await api(signup ? '/auth/signup' : '/auth/login', {
      username: $('#username').value.trim(), password: $('#password').value,
    });
    storeToken(result.access_token);
    $('#password').value = '';
    message('');
    await showAccount(result.username);
  } catch (error) { message(error.message, true); }
  finally { $('#submit-auth').disabled = false; }
});


/************
 * Terminal *
 ************/

$('#approve').addEventListener('click', async () => {
  $('#approve').disabled = true;
  message('Approving connection…');

  try {
    await api('/auth/device/approve', { user_code: code });
    finished = true;
    $('#device').hidden = true;
    $('#done').hidden = false;
    message('Computer approved.');
  } catch (error) { message(error.message, true); }
  finally { $('#approve').disabled = false; }
});

$('#logout').addEventListener('click', async () => {
  $('#logout').disabled = true;

  try {
    await api('/auth/logout', {});
    storeToken(null);
    location.reload();
  } catch (error) { message(error.message, true); }
  finally { $('#logout').disabled = false; }
});


/********
 * Load *
 ********/

if (code) $('#auth-intro').textContent = 'Sign in or create an account to connect the computer waiting in your terminal.';

/* already signed in this tab */
if (token) {
  try {
    const user = await api('/auth/me');
    await showAccount(user.username);
  } catch (error) { message(error.message, true); }
}
