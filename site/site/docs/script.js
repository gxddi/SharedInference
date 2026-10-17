const base = window.location.origin; // examples point at this site


/************
 * Examples *
 ************/

for (const code of document.querySelectorAll('pre code')) {
  code.textContent = code.textContent.replaceAll('https://YOUR-HOST', base);
}


/********
 * Tabs *
 ********/

for (const group of document.querySelectorAll('.code[data-tabs]')) {
  const tabs = group.querySelectorAll('[role="tab"]');
  const panels = group.querySelectorAll('pre');

  tabs.forEach((tab, index) => tab.addEventListener('click', () => {
    tabs.forEach((other, i) => other.setAttribute('aria-selected', String(i === index)));
    panels.forEach((panel, i) => { panel.hidden = i !== index; });
  }));
}


/********
 * Copy *
 ********/

for (const group of document.querySelectorAll('.code')) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy';
  button.textContent = 'Copy';

  button.addEventListener('click', async () => {
    const visible = [...group.querySelectorAll('pre')].find((pre) => !pre.hidden); // the open tab
    try {
      await navigator.clipboard.writeText(visible.textContent);
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Select to copy';
    }
    setTimeout(() => { button.textContent = 'Copy'; }, 1500);
  });

  group.append(button);
}
