# Font provenance

Bundled locally per the redesign's "no external font CDN" constraint. Every
file below was fetched directly from the project's official upstream and is
unmodified from that source.

## Atkinson Hyperlegible Next

- Source: https://github.com/googlefonts/atkinson-hyperlegible-next
- Commit pinned at fetch time: `7925f50f649b3813257faf2f4c0b381011f434f1` (branch `main`;
  the repo publishes no tagged GitHub releases, so the commit SHA is the
  provenance anchor)
- License: SIL Open Font License 1.1 (`OFL-Atkinson.txt`, copied unmodified
  from the repo's `OFL.txt` at the same commit)
- Files fetched from `fonts/webfonts/` in the repo (pre-built static woff2,
  not the variable font):

  | File | Source path | SHA-256 |
  |---|---|---|
  | `atkinson-hyperlegible-next-regular-400.woff2` | `fonts/webfonts/AtkinsonHyperlegibleNext-Regular.woff2` | `378aea0f5c1d179f4e0b5382c06bfc87571b98cfcc4fd1352bc979e2e2259c54` |
  | `atkinson-hyperlegible-next-bold-700.woff2` | `fonts/webfonts/AtkinsonHyperlegibleNext-Bold.woff2` | `dda449f0f556a595cffd0a9ce479bb1210beba286cb4c2f5aeca6975f9c85a3b` |

## IBM Plex Mono

- Source: https://github.com/IBM/plex
- Release: `@ibm/plex-mono@2.5.0`
  (https://github.com/IBM/plex/releases/tag/%40ibm%2Fplex-mono%402.5.0)
- Release asset: `ibm-plex-mono.zip`
- License: SIL Open Font License 1.1 (`OFL-IBMPlexMono.txt`, copied
  unmodified from the release asset's `LICENSE.txt`)
- Files extracted from the asset's `fonts/complete/woff2/` (single-file,
  unsplit woff2 -- the `fonts/split/woff2/` subset files were not used):

  | File | Source path (inside the release zip) | SHA-256 |
  |---|---|---|
  | `ibm-plex-mono-regular-400.woff2` | `ibm-plex-mono/fonts/complete/woff2/IBMPlexMono-Regular.woff2` | `ba204497f16b6d334cee9d1e963a831b73e3a56e1d6300a8489d18df7214b350` |
  | `ibm-plex-mono-semibold-600.woff2` | `ibm-plex-mono/fonts/complete/woff2/IBMPlexMono-SemiBold.woff2` | `6a825b4824c01cbb401e829e5a066a1818411bcb3538b5a5792c5ca9b82343c3` |

## Fraunces

- Source: https://github.com/undercasetype/Fraunces
- Fetched via Fontsource (a faithful self-host packager: static instances
  generated from the upstream release, unmodified glyph data, latin subset),
  package `@fontsource/fraunces`, resolved version `5.3.0`
  (https://cdn.jsdelivr.net/npm/@fontsource/fraunces@5.3.0/files/)
- License: SIL Open Font License 1.1 (`OFL-Fraunces.txt`, copied unmodified
  from the upstream repo's `OFL.txt` at `master`)
- Files fetched from the package's `files/` directory (static instances, not
  the variable font):

  | File | Source path | SHA-256 |
  |---|---|---|
  | `fraunces-semibold-600.woff2` | `files/fraunces-latin-600-normal.woff2` | `3a1de7711d147bad4422825045f87597fd77cca72e7c96d3b0a81735d00dda82` |
  | `fraunces-italic-400.woff2` | `files/fraunces-latin-400-italic.woff2` | `475b154af19c6b4ef371db22078b5044a078ce2467e11ef22a7b87f77837caf3` |

## Young Serif

- Source: https://github.com/noirblancrouge/YoungSerif
- Fetched from Google Fonts' self-host mirror (the static woff2 Google serves;
  the upstream repo publishes no pre-built woff2)
- License: SIL Open Font License 1.1 (`OFL-YoungSerif.txt`, copied unmodified
  from `github.com/google/fonts/ofl/youngserif/OFL.txt`)

  | File | Source | SHA-256 |
  |---|---|---|
  | `young-serif-regular-400.woff2` | `fonts.gstatic.com/s/youngserif/v2/...woff2` (latin) | `cb3d88bfd03d56f1a73bbd06884f021ec04ca1bf7e50df7f2d10c958822015bf` |

Fetched 2026-08-02 (Atkinson Hyperlegible Next, IBM Plex Mono); Fraunces
fetched 2026-08-03; Young Serif fetched 2026-08-05.
