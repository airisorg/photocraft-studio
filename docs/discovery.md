# Discovery and presentation

This guide records the repository and public-page choices researched on
2026-10-08. They make the project easier to understand and share; they do not
promise search rankings, indexing times or stars.

## GitHub

The repository name is `airisorg/photocraft-studio`. Its description identifies a
Rust/WebAssembly image editor, the independent PhotoCraft fork and Tofu hosting.
The homepage opens the actual editor. Topics describe the implementation and
supported use: `image-editor`, `photo-editor`, `raster-graphics`, `digital-painting`,
`rust`, `webassembly`, `egui`, `wgpu`, `webgpu`, `webgl`, `collaboration`, `real-time`,
`axum`, `postgresql`, `photocraft`, `tofu` and `trytofu`.

GitHub permits up to 20 topics, each at most 50 characters. Default repository
search considers the name, description and topics; use `in:readme` to search the
README. Private repositories appear only to viewers who can access them.
[Topic guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics),
[repository search](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories).

## README and media

The opening explains the product, credits the original editor and states the
fork's independence, then introduces Tofu. It provides a live-editor link, one
real screenshot, an optional short GIF, the additions, quick start, code map and
licenses. This follows GitHub's guidance to explain what a project does and how
to start, while keeping the deployment story visible.
[README guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes).

The real screenshot stays useful without animation. The GIF is optional, and
its nearby text explains the demonstrated move and Undo/Redo behavior. GitHub
users can disable GIF autoplay through accessibility settings.
[Motion settings](https://docs.github.com/en/account-and-profile/how-tos/account-settings/managing-accessibility-settings).

`docs/media/social-preview.png` is a 1280 × 640 PNG below 1 MB. Upload it using
**Settings → Social preview** when the repository is eligible; adding the image
to Git does not configure GitHub's social preview. GitHub documents eligibility
and limits separately from ordinary README images.
[Social-preview guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).

## Web search and link previews

The editor renders through a canvas. Googlebot does not support WebGL, so the
public `/about.html` page presents readable HTML, real screenshots, useful links,
upstream credit and the Tofu deployment story without loading the editor or
checking an account. Its title, description and canonical URL describe that
page. The editor keeps generic Open Graph metadata and `noindex, follow`;
private project content is not used as search or preview metadata.
[Rendering limitations](https://developers.google.com/search/docs/crawling-indexing/javascript/fix-search-javascript),
[title guidance](https://developers.google.com/search/docs/appearance/title-link),
[canonical guidance](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls).

Open Graph metadata points to the real social card, with its dimensions and alt
text. It helps link previews; it is not a ranking promise. Google does not use
keyword meta tags, and the page adds no fabricated ratings or reviews.
[Open Graph protocol](https://ogp.me/),
[supported meta tags](https://developers.google.com/search/docs/crawling-indexing/special-tags),
[structured-data policy](https://developers.google.com/search/docs/appearance/structured-data/sd-policies).

After public release, verify anonymous README/media access and observed GitHub
search results. Search Console ownership and URL inspection remain owner steps;
no Search Console account or sitemap submission is configured by these files.
Check [the publication policy](../SECURITY.md) before changing repository visibility.

## Verify the presentation

`tests/web/test_discovery.py` checks the initial HTML, generic preview metadata,
Trunk copy paths and byte-preserving packaging. `tests/web/test_discovery_browser.py`
opens the actual public page in eight fresh browser contexts at 320, 390, 768
and 1440 pixels. It checks readable text with JavaScript disabled, contrast,
clipping, 44-pixel action targets and keyboard focus, and retains screenshots.
It permits only public GET requests and never signs in or creates test accounts.

The browser test defaults to the disposable local service used by CI. An operator
can set `PHOTOCRAFT_TEST_ORIGIN` to the deployed HTTPS origin and
`PHOTOCRAFT_TEST_ARTIFACTS` to a separate evidence directory to verify the served
page. Passing these checks does not establish search indexing or rankings.
