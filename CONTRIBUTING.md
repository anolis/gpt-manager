# Contributing

Use Node.js 24 and Python 3.10 or newer. Run `npm install`, `npm run check`, and `npm test` before submitting a pull request.

Provider adapters belong in `backend/manager.py`; resume commands and process ownership belong in `desktop/terminals.cjs`. Keep the renderer sandboxed and expose narrowly scoped preload methods. Treat conversation contents and imported manifests as untrusted data.

Use synthetic fixtures in tests and screenshots. Do not commit provider histories, credentials, private project paths, exported context bundles, or manager data directories. Add regression tests for changes to archive validation, restore behavior, parsing, and terminal lifecycle. Native resume must preserve provider approval controls.

Store formats can change. Describe which provider/version or schema your change supports, and preserve original files when the viewer cannot interpret them.
