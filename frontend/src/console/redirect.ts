// The console's old address (docs/50): /dashboard/console.html#<place> opens the one page at
// /dashboard/ on the same place. A meta refresh would drop the hash, so this is a script: a
// file of the gateway's, never inline, under the page's content security policy. Without
// JavaScript the page's <noscript> refresh and its link still lead to /dashboard/.

window.location.replace(new URL(`./${window.location.hash}`, window.location.href).href);
