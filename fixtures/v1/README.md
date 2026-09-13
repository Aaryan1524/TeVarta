# API fixtures, v1

These files are the fixture-first boundary for T03. The FastAPI mock server and
the Swift decoding tests load these exact JSON files. Do not change a response
shape in only one client.

`scenario_manifest.json` records the client state represented by each fixture.
`loading` is a client state, not a production API response; the mock exposes it
only when `scenario=loading` is explicitly requested.
