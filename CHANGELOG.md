# Changelog

## 1.1.0

- Add selected-window peak download/upload to each connection card.
- Retain recently active adapters in the cards while their samples remain in the selected window, even if the adapter disconnects.
- Include both peak download and peak upload for the combined tunnel.
- Keep all peak calculations consistent with the selected or paused 2-, 5-, or 15-minute chart window.
- Store per-connection samples within the existing bounded, in-memory history.
- Make the installer startup check read the application version instead of assuming the original version number.
- Add repeatable frontend checks and GitHub Actions configuration for public development.

## 1.0.0

- Initial local dashboard with combined traffic graphs, internet ping, per-connection current rates/RTT, and current server details.
- Read-only LAN HTTP service using Python's standard library.
- Fifteen minutes of in-memory history, reconnect handling, and systemd installation.
