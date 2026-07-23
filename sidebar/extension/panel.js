// ponytail: tab is captured once at panel-open; claude's system prompt can't change mid-session.
// Live tab-switch awareness = restart claude or feed via MCP; add if it matters.
chrome.tabs.query({ active: true, lastFocusedWindow: true }, ([tab]) => {
  const boot =
    'You are Claude Code embedded in a Chrome side panel. ' +
    'This is the browser tab you have access to talk to (drive it with the claude-in-chrome MCP tools): ' +
    `tabId=${tab.id}, windowId=${tab.windowId}, title=${JSON.stringify(tab.title ?? '')}, url=${tab.url}. ` +
    'Operate on this tab unless told otherwise.';
  const q = new URLSearchParams();
  q.append('arg', '--append-system-prompt');
  q.append('arg', boot);
  document.getElementById('term').src = 'http://127.0.0.1:7681/?' + q.toString();
});
