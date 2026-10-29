# Third-party notices

Run·dere is licensed under the [GNU AGPL-3.0](LICENSE). It includes, or is built on, the
work of others under their own licences, listed here. Each licence's full text is in the
file named, or in the package as installed.

## Bundled in this repository

| What | Where | Licence | Notes |
|---|---|---|---|
| [htmx](https://htmx.org/) 2.0.4 | `app/review/static/htmx.min.js` | 0BSD (Zero-Clause BSD) | |
| [Leaflet](https://leafletjs.com/) 1.9.4 | `app/review/static/vendor/leaflet/` (`leaflet.js`, `leaflet.css`, `images/`) | BSD-2-Clause, Copyright (c) 2010-2023 Volodymyr Agafonkin, (c) 2010-2011 CloudMade | `LICENSE-leaflet.txt`; the Places Map lens (D-218) |
| [Leaflet.markercluster](https://github.com/Leaflet/Leaflet.markercluster) 1.5.3 | `app/review/static/vendor/leaflet/` (`leaflet.markercluster.js`, `MarkerCluster*.css`) | MIT, Copyright 2012 David Leaver | `LICENSE-markercluster.txt` |
| [Figtree](https://github.com/erikdkennedy/figtree) | `design/fonts/figtree-*` | SIL Open Font License 1.1 | `design/fonts/LICENSE-figtree.txt` |
| [Outfit](https://github.com/Outfitio/Outfit-Fonts) | `design/fonts/outfit-*` | SIL Open Font License 1.1 | `design/fonts/LICENSE-outfit.txt` |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | `design/fonts/jetbrains-mono-*` | SIL Open Font License 1.1 | `design/fonts/LICENSE-jetbrains-mono.txt` |
| Airport reference data from [mwgg/Airports](https://github.com/mwgg/Airports) | `resolve/data/airports.json.gz` | MIT, Copyright (c) 2014 mwgg | `resolve/data/AIRPORTS-LICENSE`; rebuilt by `scripts/build_airports.py` |
| Phrasebook entries, seeded from [Wikivoyage](https://en.wikivoyage.org/) phrasebooks and edited | `discover/data/phrases.json` | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) | Attribution: Wikivoyage contributors. This file, as changed, is shared under the same licence |
| Plug types and mains voltage by country | `discover/data/plugs.json` | Facts | Compiled from the IEC's World Plugs reference |

The Run·dere name, logos, icons and audio in `design/` are the project's own and are
covered by this repository's licence.

## Services the server uses at runtime

Nothing below is bundled. The server calls these over the network when it's configured
to, and each has its own terms.

| Service | Used for | Terms |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) tiles (default `TX_MAP_TILE_URL`) | Trip maps, cached on your server | Map data © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/). Tiles are subject to the [OSMF tile usage policy](https://operations.osmfoundation.org/policies/tiles/); set your own tile server for heavy use. Run·dere shows the attribution on its maps |
| [Open-Meteo](https://open-meteo.com/) | Weather in Discover | Data under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), "Weather data by Open-Meteo.com" |
| [Frankfurter](https://frankfurter.dev/) | Currency rates in Discover | Free API over European Central Bank reference rates |
| [Photon](https://github.com/komoot/photon) (self-hosted) | Geocoding | Apache-2.0 software; its index is OpenStreetMap data (ODbL) |
| [Ollama](https://ollama.com/), [SearXNG](https://github.com/searxng/searxng) (self-hosted) | The local LLM and web search | Each under its own licence, and the licence of the model you choose |

## Python dependencies

The packages the server installs at runtime (`uv.lock`, without development tools),
as declared in each package's metadata. `scripts/prepublish.py` checks on every run that
each one is compatible with the AGPL-3.0.

| Package | Version | Licence |
|---|---|---|
| alembic | 1.19.1 | MIT |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.14.2 | MIT |
| attrs | 26.1.0 | MIT |
| babel | 2.18.0 | BSD |
| cffi | 2.1.1 | MIT-0 |
| click | 8.4.2 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| cssselect2 | 0.10.1 | BSD |
| fastapi | 0.141.1 | MIT |
| flatbuffers | 25.12.19 | Apache-2.0 |
| fonttools | 4.66.1 | MIT |
| greenlet | 3.5.5 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| h3 | 4.5.0 | Apache-2.0 |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx2 | 2.13.1 | BSD-3-Clause |
| idna | 3.18 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| Mako | 1.4.1 | MIT |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| mcp | 2.3.0 | MIT |
| mcp-types | 2.3.0 | MIT |
| numpy | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| pillow | 12.3.0 | MIT-CMU |
| psycopg | 3.3.4 | LGPL-3.0-only |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pydantic_core | 2.46.4 | MIT |
| pydyf | 0.12.1 | BSD |
| PyJWT | 2.15.1 | MIT |
| pypdf | 6.20.1 | BSD-3-Clause |
| pyphen | 0.18.1 | MPL-1.1 OR GPL-2.0-or-later OR LGPL-2.1-or-later |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| sqladmin | 0.31.0 | BSD-3-Clause |
| SQLAlchemy | 2.0.52 | MIT |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.6.0 | BSD-3-Clause |
| timezonefinder | 8.2.5 | MIT |
| tinycss2 | 1.5.1 | BSD |
| tinyhtml5 | 2.1.0 | MIT |
| truststore | 0.10.4 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.52.3 | BSD-3-Clause |
| weasyprint | 70.0 | BSD |
| webencodings | 0.6.1 | BSD |
| WTForms | 3.2.2 | BSD |
