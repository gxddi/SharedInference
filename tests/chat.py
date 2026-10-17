#!/usr/bin/env python3
"""Single-file local chat website for the project's OpenAI-compatible API."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SharedInference · Chat</title>
<style>
:root { color-scheme: dark; font: 15px/1.5 system-ui, sans-serif; background: #101214; color: #e8ecef; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 960px; height: 100dvh; display: flex; flex-direction: column; padding: 0 24px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 24px 0; }
h1 { font-size: 18px; margin: 0; font-weight: 600; }
.subtitle, label, #status, .empty { color: #a0a9b2; font-size: 13px; }
#rate { text-align: right; white-space: nowrap; font-size: 22px; font-variant-numeric: tabular-nums; }
#rate small { color: #a0a9b2; font-size: 13px; }
#settings { display: flex; align-items: end; gap: 12px; padding-bottom: 16px; border-bottom: 1px solid #30363d; }
label { display: flex; flex-direction: column; gap: 6px; flex: 1; min-width: 0; }
input, select, textarea, button { font: inherit; color: inherit; border: 1px solid #394149; border-radius: 8px; padding: 10px 12px; background: #1a1f24; }
input, select { width: 100%; height: 44px; }
button { cursor: pointer; white-space: nowrap; background: #d9f3c6; color: #172212; border-color: #d9f3c6; }
button:disabled { opacity: .45; cursor: default; }
:focus-visible { outline: 2px solid #b8e794; outline-offset: 2px; }
#status { margin: 10px 0; min-height: 20px; overflow-wrap: anywhere; }
#status.error { color: #ffadad; }
#messages { flex: 1; overflow-y: auto; min-height: 0; padding: 12px 0; }
.empty { text-align: center; margin-top: 15vh; }
.message { padding: 16px; margin-bottom: 12px; border-radius: 10px; background: #191e23; overflow-wrap: anywhere; }
.message.user { background: #212b21; }
.message strong { display: block; font-size: 12px; color: #b8c2cc; margin-bottom: 6px; }
.message p { margin: 0; white-space: pre-wrap; }
#compose { display: flex; gap: 12px; padding: 16px 0 24px; }
textarea { flex: 1; resize: vertical; min-height: 70px; max-height: 180px; }
#compose button { align-self: end; }
@media (max-width: 600px) { body { padding: 0 14px; } #settings { flex-wrap: wrap; } #settings label { flex-basis: 100%; } header { gap: 12px; } }
</style>
</head>
<body>
<header>
  <div><h1>SharedInference</h1><div class="subtitle">Chat test</div></div>
  <div id="rate" title="Completion tokens / request seconds, including network latency and prompt processing" aria-live="polite">— <small>tok/s</small></div>
</header>
<form id="settings">
  <label>API key<input id="key" type="password" autocomplete="off" placeholder="Paste your API key" required></label>
  <button id="load" type="submit">Load models</button>
  <label>Model<select id="model" disabled><option value="">Load models first</option></select></label>
</form>
<p id="status" role="status">Enter your API key to connect. Keys and chat history stay in memory.</p>
<main id="messages" aria-label="Conversation" aria-live="polite"><p class="empty">Select a model, then start chatting.</p></main>
<form id="compose">
  <textarea id="prompt" aria-label="Message" placeholder="Message… (Enter to send, Shift+Enter for a new line)" disabled required></textarea>
  <button id="send" type="submit" disabled>Send</button>
</form>
<script>
const $ = id => document.getElementById(id);
let key = '', messages = [], busy = false;
function status(text, error = false) {
  $('status').textContent = text;
  $('status').classList.toggle('error', error);
}
function controls() {
  $('key').disabled = busy;
  $('load').disabled = busy;
  $('model').disabled = busy || !key;
  $('prompt').disabled = busy || !key || !$('model').value;
  $('send').disabled = $('prompt').disabled;
}
function reset() {
  messages = [];
  $('messages').replaceChildren();
  $('rate').innerHTML = '— <small>tok/s</small>';
}
function bubble(role, text) {
  const item = document.createElement('article');
  item.className = 'message ' + role;
  const heading = document.createElement('strong');
  heading.textContent = role === 'user' ? 'You' : 'Assistant';
  const content = document.createElement('p');
  content.textContent = text;
  item.append(heading, content);
  $('messages').append(item);
  $('messages').scrollTop = $('messages').scrollHeight;
  return item;
}
async function api(path, data, token = key) {
  const response = await fetch(path, {
    method: data ? 'POST' : 'GET',
    headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || 'Request failed.');
  return result;
}
$('key').addEventListener('input', () => {
  key = '';
  $('model').replaceChildren(new Option('Load models first', ''));
  reset();
  controls();
});
$('settings').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const candidate = $('key').value.trim();
  if (!candidate) return;
  busy = true;
  controls();
  status('Loading available models…');
  try {
    const result = await api('/api/models', null, candidate);
    key = '';
    $('model').replaceChildren(new Option('Select a model…', ''));
    reset();
    for (const model of result.data) $('model').append(new Option(model.id, model.id));
    if (result.data.length) {
      key = candidate;
      status('Select a model to start chatting.');
    } else status('No models are ready. Connect nodes and load a model first.');
  } catch (error) { status(error.message, true); }
  finally { busy = false; controls(); }
});
$('model').addEventListener('change', () => {
  reset();
  controls();
  status($('model').value ? 'Ready. Replies are limited to 256 tokens.' : 'Select a model.');
  if ($('model').value) $('prompt').focus();
});
$('compose').addEventListener('submit', async event => {
  event.preventDefault();
  const prompt = $('prompt').value.trim();
  if (busy || !key || !$('model').value || !prompt) return;
  const conversation = [...messages, {role: 'user', content: prompt}];
  const userBubble = bubble('user', prompt);
  busy = true;
  controls();
  status('Generating…');
  try {
    const result = await api('/api/chat', {model: $('model').value, messages: conversation, max_tokens: 256, stream: false});
    const answer = result.choices[0].message.content;
    if (typeof answer !== 'string') throw new Error('Invalid chat response.');
    messages = [...conversation, {role: 'assistant', content: answer}];
    bubble('assistant', answer);
    $('prompt').value = '';
    const timing = result.timing;
    $('rate').innerHTML = (timing.tokens_per_second === null ? '—' : timing.tokens_per_second.toFixed(2)) + ' <small>tok/s</small>';
    status(`${result.usage?.completion_tokens ?? 'Unknown'} output tokens · ${timing.seconds.toFixed(2)} s · rate includes latency and prompt processing`);
  } catch (error) { userBubble.remove(); status(error.message, true); }
  finally { busy = false; controls(); $('prompt').focus(); }
});
$('prompt').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('compose').requestSubmit();
  }
});
</script>
</body>
</html>'''


def request(base_url, key, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request(base_url.rstrip('/') + path, data=body, headers={
        'Authorization': key, 'Content-Type': 'application/json',
    })
    try:
        with urlopen(req, timeout=300) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc)['error']['message']
        except (ValueError, KeyError, TypeError):
            message = exc.reason
        raise RuntimeError(f'HTTP {exc.code}: {message}') from None
    except (URLError, OSError) as exc:
        raise RuntimeError(f'Cannot reach {base_url}: {exc}') from None
    except ValueError:
        raise RuntimeError('The server returned invalid JSON.') from None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Keep keys and chat content out of logs.

    def reply(self, status, data, content_type='application/json'):
        body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/':
            return self.reply(200, PAGE, 'text/html; charset=utf-8')
        if self.path == '/api/models':
            return self.proxy('/models')
        self.reply(404, {'error': {'message': 'Not found.'}})

    def do_POST(self):
        if self.path != '/api/chat':
            return self.reply(404, {'error': {'message': 'Not found.'}})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2**20:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, TypeError):
            return self.reply(400, {'error': {'message': 'Invalid request body.'}})
        data['stream'] = False
        self.proxy('/chat/completions', data)

    def proxy(self, path, data=None):
        key = self.headers.get('Authorization', '')
        if not key.startswith('Bearer ') or not key[7:].strip():
            return self.reply(401, {'error': {'message': 'An API key is required.'}})
        try:
            started = time.monotonic()
            result = request(self.server.base_url, key, path, data)
            elapsed = time.monotonic() - started
            if data is not None:
                tokens = result.get('usage', {}).get('completion_tokens')
                result['timing'] = {
                    'seconds': elapsed,
                    'tokens_per_second': tokens / elapsed if isinstance(tokens, int) and elapsed > 0 else None,
                }
            self.reply(200, result)
        except RuntimeError as exc:
            self.reply(502, {'error': {'message': str(exc)}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://66-42-90-154.sslip.io:1025/api/v1',
                        help='Upstream API base URL (default: https://66-42-90-154.sslip.io:1025/api/v1)')
    parser.add_argument('--port', type=int, default=8765, help='Local website port (default: 8765)')
    args = parser.parse_args()
    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        server.base_url = args.base_url
        print(f'Chat: http://localhost:{server.server_port}', flush=True)
        print(f'API: {args.base_url}\nPress Ctrl-C to stop.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
