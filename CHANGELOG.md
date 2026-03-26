# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Security

- **Dockerfile**: container now runs as a non-root `app` system user instead of root (`missing-user` semgrep rule).
- **Templates**: CDN `<script>` tags in `base.html` and `setup.html` now include `integrity` (SRI sha384) and `crossorigin="anonymous"` attributes; Tailwind CDN URL pinned to version 3.4.17 (`missing-integrity` semgrep rule).
- **Routes**: all `Jinja2Templates` instances initialised with explicit `autoescape=True` in `anime.py`, `auth.py`, and `settings_routes.py` (XSS semgrep rule).
