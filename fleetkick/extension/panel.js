const $ = (id) => document.getElementById(id);
let current = null;

function bars(tab) {
  $('title').textContent = tab.title || '';
  $('url').textContent = tab.url || '';
  $('ids').textContent = `tab ${tab.id} · window ${tab.windowId}`;
  $('fav').hidden = !tab.favIconUrl;
  if (tab.favIconUrl) $('fav').src = tab.favIconUrl;
}

// Pair the panel to a tab: bars + terminal both derive from the same tab object,
// and the server keeps one warm tmux per tabId, so switching back reattaches instantly.
function pair(tab) {
  if (!tab || tab.id === undefined) return;
  const changed = !current || current.id !== tab.id;
  current = tab;
  $('dot').classList.remove('dead');
  bars(tab);
  if (changed) {
    const q = new URLSearchParams();
    q.append('arg', tab.id);
    q.append('arg', tab.windowId);
    $('term').src = 'http://127.0.0.1:7681/?' + q.toString();
  }
}

chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => pair(tabs[0]));
chrome.tabs.onActivated.addListener((info) => chrome.tabs.get(info.tabId, pair));
chrome.tabs.onUpdated.addListener((id, _info, tab) => {
  if (current && id === current.id) bars(tab);
});
chrome.tabs.onRemoved.addListener((id) => {
  if (current && id === current.id) {
    $('dot').classList.add('dead');
    $('title').textContent = ($('title').textContent || '') + ' (closed)';
  }
});
