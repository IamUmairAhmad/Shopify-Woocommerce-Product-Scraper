// Opens the exporter in a tab. If the current tab is a web page, its address is filled in.
chrome.action.onClicked.addListener((tab) => {
  let url = chrome.runtime.getURL("app.html");
  if (tab && tab.url && /^https?:\/\//.test(tab.url)) url += "?store=" + encodeURIComponent(tab.url);
  chrome.tabs.create({ url });
});
