# Third-party notices and licensing status

The original Python bridge and repository documentation are licensed under the MIT License, Copyright 2026 RerrentLinden. See `LICENSE`.

Tencent/openclaw-weixin (MIT, Copyright 2026 Tencent) is the protocol reference for media upload/encryption/message fields. The bridge implements that protocol in Python; no Tencent TypeScript source is bundled. Its license text is retained for attribution in `licenses/Tencent-openclaw-weixin-MIT.txt`. Reference revision: `24de5c9eb0dd5e595d7e2d090ed8a3f82870d42c`.

Lawrence's [wx-bot](https://github.com/lawrencewzen/wx-bot) (MIT) is a static reference for event dispatch, webhook leases and timestamp-recording ideas in `run.ts`, `inbox.ts` and `protocol.md`. Reference commit: `2fd366a60802a230583cebc89add2fb1c660ce8c`; its license text is retained in `licenses/wx-bot-MIT.txt`. The Python adaptations in this candidate are the event pump and lease compare-and-swap in `probe/probe.py`, after-commit wakeup hooks in `probe/weixin.py` and `probe/media.py`, and metadata timestamps in `probe/transport_timing.py`. No wx-bot executable components are bundled or run, and the existing Weixin sender is retained. The candidate does not adopt its random `client_id`, 100 MB limit increases or automatic stall-watch notifications.

This historical code reference does not identify the separate “WX-BOT” experience reference mentioned by the user; no URL or license for that reference was supplied, and no code from an unidentified project is included.

OpenAI/tunnel-client is installed separately from its official distribution. Follow that distribution's LICENSE and included third-party notices. This repository does not relicense or redistribute it.

Python dependencies are installed separately from their publishers: cryptography (Apache-2.0 OR BSD-3-Clause), Pillow (MIT-CMU), cffi (MIT-0), pycparser (BSD-3-Clause), qrcode (BSD-3-Clause). The pinned distributions include their own notices. No dependency binaries are bundled here.

This directory distributes singletrial1 candidate source and its installation Skill. Offline checks do not establish production or end-to-end delivery acceptance. Public exports must exclude deployment credentials, private message history and runtime state, and pass static-allowlist, scanner and manual review before release; these checks are not a complete guarantee against sensitive information. See [docs/PUBLISHING.md](docs/PUBLISHING.md).

The static historical source fixtures under `probe_tests/fixtures/rollback_baseline`, `localcutover_baseline`, and `guarded_rollback` are included only for offline rollback and compatibility tests. Their license and notice files remain in each fixture directory; all three groups have reviewed file hashes and version provenance in `probe_tests/fixtures/FIXTURE-PROVENANCE.json`. They are not installable rollback distributions.

This independent project is not affiliated with or endorsed by OpenAI or Tencent.
