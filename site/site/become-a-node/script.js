const shellQuote = (value) => "'" + value.replaceAll("'", "'\\''") + "'";
const server = window.location.origin; // this site, so the installer knows what to join
const repository = 'https://raw.githubusercontent.com/gxddi/SharedInference/main';


/************
 * Commands *
 ************/

document.querySelector('#install-command').textContent =
  `curl -fsSL ${shellQuote(repository + '/scripts/install.sh')} | SINFERENCE_SERVER=${shellQuote(server)} sh`;

document.querySelector('#up-command').textContent = '~/.local/bin/sinference up'; // the installer remembers this site


/********
 * Copy *
 ********/

for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent);
      document.querySelector('#copy-status').textContent = 'Command copied.';
    } catch {
      document.querySelector('#copy-status').textContent = 'Select and copy the command above.';
    }
  });
}
