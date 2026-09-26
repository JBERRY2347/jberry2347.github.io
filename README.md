# jberry2347.github.io — the tools hub

This repo is the server. GitHub Pages serves it at **https://jberry2347.github.io/**, and the
page there is a launcher for every tool I've built. Each tool is its own repo with Pages
enabled, so it lives at `https://jberry2347.github.io/<repo>/` on the same domain as the hub.

```
https://jberry2347.github.io/                ← this repo (user site): the hub
https://jberry2347.github.io/cuesight/       ← JBERRY2347/cuesight  (project site)
https://jberry2347.github.io/holdtheline/    ← JBERRY2347/holdtheline (project site)
```

No build step, no backend. The hub is a static page that reads `tools.json` and renders a card
per tool with an Open button, source link, privacy link, tags and a category filter. It's
installable as a PWA and works offline once visited.

## Files

| File | Purpose |
|---|---|
| `index.html` | The launcher. Fetches `tools.json` and renders the grid. |
| `tools.json` | **The registry.** One entry per tool. Edit this to add, reorder or retire a tool. |
| `tools.schema.json` | JSON Schema for the registry, for editor validation. |
| `manifest.json`, `sw.js`, `icon*.png`, `icon.svg` | PWA bits for the hub itself. |
| `404.html` | Fallback for bad URLs under the root. |
| `.well-known/assetlinks.json` | Digital Asset Links for the CueSight Android app (TWA). Keep this. |
| `.nojekyll` | Tells Pages to serve files as-is (no Jekyll). |

## Adding a tool

1. Push the tool to its own public repo under `JBERRY2347/<name>`. A single `index.html` is enough.
2. In that repo: **Settings → Pages → Source: Deploy from a branch → `main` / `(root)`**.
   It will be live at `https://jberry2347.github.io/<name>/` in a minute or two.
3. Add an entry to `tools.json`:

   ```json
   {
     "id": "<name>",
     "name": "Display Name",
     "tagline": "One short line",
     "description": "A sentence or two.",
     "url": "./<name>/",
     "repo": "https://github.com/JBERRY2347/<name>",
     "icon": "./<name>/icon-192.png",
     "category": "Utilities",
     "tags": ["camera", "offline"],
     "status": "live"
   }
   ```

   Only `id`, `name`, `tagline` and `url` are required. `url` can also be an absolute link if a
   tool is hosted somewhere else. `status` is one of `live`, `beta`, `wip`, `archived`;
   archived tools are hidden unless you open the hub with `#all`.
4. Commit and push. The hub picks it up on the next load.

If the tool has its own `manifest.json`, give it `"id": "/<name>/"` and `"scope": "./"` (see
the CueSight and Hold the Line manifests) so it installs as a separate app from the hub.

## Running it locally

Any static file server works. From this directory:

```sh
python3 -m http.server 8080
# then open http://localhost:8080/
```

To see the tool cards with real icons and working Open buttons, clone the tool repos into
subfolders named after them (`cuesight/`, `holdtheline/`) or symlink them. Those folders are
git-ignored, so they stay out of this repo.

## Android

`.well-known/assetlinks.json` lets the CueSight Trusted Web Activity open
`https://jberry2347.github.io/cuesight/` without the browser bar. Add another entry there
if you ship another tool as an Android app.
