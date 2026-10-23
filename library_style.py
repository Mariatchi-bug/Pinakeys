"""Shared, asset-free appearance for the desktop launcher and offline pages."""

PALETTE = {
    'paper': '#f6f5f1',
    'surface': '#ffffff',
    'ink': '#203c35',
    'text': '#34423c',
    'muted': '#66736c',
    'rule': '#dcddd5',
    'accent': '#245c4a',
    'accent_hover': '#194535',
    'wash': '#eaf0ea',
    'review': '#975500',
    'review_surface': '#fff8ec',
}

BASE_CSS = ':root {' + ''.join(
    f'--{name.replace("_", "-")}:{value};' for name, value in PALETTE.items()
) + '''
color-scheme:light;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
color:var(--text);background:var(--paper);font-size:16px;
}
* {box-sizing:border-box}
body {max-width:1280px;margin:0 auto;padding:24px 42px 48px;line-height:1.5}
.site-header {display:flex;align-items:center;justify-content:space-between;gap:20px;
  padding:0 0 20px;margin-bottom:28px;border-bottom:1px solid var(--rule)}
.brand {font-family:Georgia,"Times New Roman",serif;font-size:40px;font-weight:700;
  line-height:1.12;letter-spacing:-1.6px;color:var(--ink);text-decoration:none}
.header-link {border:1px solid var(--muted);border-radius:4px;padding:9px 20px;
  color:var(--ink);font-size:15px;font-weight:600;text-decoration:none;white-space:nowrap}
.header-link:hover {background:var(--wash)}
h1 {font-family:Georgia,"Times New Roman",serif;font-size:clamp(34px,4.1vw,52px);
  line-height:1.16;font-weight:700;letter-spacing:-1.6px;color:var(--ink);margin:0 0 8px}
h2 {color:var(--ink)}
.lede {color:var(--muted);font-size:17px;margin:0 0 24px;overflow-wrap:anywhere}
.muted {color:var(--muted)}
a {color:var(--accent);text-underline-offset:4px;text-decoration-thickness:1px}
a:hover {color:var(--accent-hover)}
a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible {
  outline:3px solid var(--accent);outline-offset:4px}
.stats {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:24px 0 28px;
  gap:0;align-items:center}
.stat {position:relative;padding:14px 18px;text-align:center;min-width:0}
.stat + .stat:before {content:"";position:absolute;left:0;top:14px;bottom:14px;
  border-left:1px solid var(--rule)}
.stat strong {display:block;font-size:38px;font-weight:650;line-height:1.2;
  color:var(--ink);font-variant-numeric:tabular-nums}
.stat small {display:block;font-size:16px;color:var(--muted);margin-top:2px}
.stat-review {color:var(--review);background:var(--review-surface);border:1px solid #e5c58d;
  border-radius:5px;margin-left:24px;padding:14px 10px;min-width:0}
.stat-review strong,.stat-review small {color:var(--review)}
button.stat-review {font:inherit;cursor:pointer;width:calc(100% - 24px)}
button.stat-review:hover,button.stat-review[aria-pressed=true] {background:#f9e9cc}
.stat-review:before {display:none}
.view-links {display:flex;flex-wrap:wrap;gap:22px;margin:0 0 28px;font-size:16px}
.view-links a + a {color:var(--muted)}
.pill {font-size:11px;line-height:1.5;background:var(--wash);color:var(--muted);
  padding:3px 7px;border-radius:4px;display:inline-block}
.warning {color:var(--review);font-weight:550}
.ok {color:var(--muted)}
footer {font-size:12px;color:var(--muted);margin-top:24px;line-height:1.65;
  border-top:1px solid var(--rule);padding-top:18px;overflow-wrap:anywhere}
@media(max-width:700px) {
  body {padding:20px 18px 36px}
  .site-header {margin-bottom:24px;padding-bottom:18px;gap:12px}
  .brand {font-size:32px}
  .header-link {font-size:13px;padding:8px 12px}
  h1 {font-size:36px;letter-spacing:-1px}
  .lede {font-size:14px}
  .stats {grid-template-columns:repeat(2,minmax(0,1fr));row-gap:16px;margin:20px 0 26px}
  .stat strong {font-size:28px}
  .stat small {font-size:13px}
  .stat:nth-child(3):before {display:none}
  .stat-review {margin-left:12px}
  button.stat-review {width:calc(100% - 12px)}
}
'''
