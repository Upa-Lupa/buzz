# Sheens Assistant local runner

## Ownership

`buzz/assistant-runner` owns the launch script, shared business prompt, attachment MCP server, document parsers and document-reading skill.
`buzz/crates/buzz-acp` supplies the authenticated conversation ID to each session's MCP processes.
`buzz/crates/buzz-agent` discovers the deployed skills and exposes `load_skill` to the model.
`sheens-erp-2` remains the source of truth for ERP tools, permissions, draft/confirmation logic and `skills/order-registration/SKILL.md`.
Do not maintain another hand-edited copy of the ERP skill here.

`deploy_skills.py` stages both skill sources into `.runtime/.agents/skills` and records their SHA-256 values in `.runtime/skills-manifest.json`.
Every newly created Assistant session uses this common catalog, without installation by individual users.
Both short core skills are also included verbatim in the generated standing system prompt, so applying mandatory business rules does not depend on the model choosing to call `load_skill`.
Restart after deploying a skill update so existing sessions discover the new version.
Local `.runtime`, `.venv` and credentials are not committed.

## Install and start locally

Requires Python 3.11+, built `target/debug/buzz-acp` and `target/debug/buzz-agent`, the sibling ERP checkout and the existing `.env.sheens-assistant.local` configuration.
The document service accepts hexadecimal and checksum-validated NIP-19 `nsec` Buzz private keys.
The ACP harness passes `nsec` to managed MCP processes even when local configuration uses hex.

```bash
python3 -m venv assistant-runner/.venv
assistant-runner/.venv/bin/pip install -r assistant-runner/requirements.txt
cargo build -p buzz-acp
./assistant-runner/start-local
```

Stop the previous Assistant process before starting this one; do not run two listeners with the same agent identity.
The launcher retains the existing ERP MCP command and all user allowlist/owner settings, adds the document MCP, deploys common skills, and sets the business system prompt.
It does not modify the user's secret env file or broaden access.
The relay and ERP containers must already be running.
The existing local ERP wrapper still selects a fixed ERP account; production requester-to-ERP identity delegation is a separate prerequisite.
Common skills do not grant ERP permissions or make the single-account local configuration safe for all production users.

## Document tools

| Tool | Purpose |
| --- | --- |
| `list_attachments` | Find signed `imeta` attachments on a message in the current conversation. |
| `inspect_attachment` | Verify/download the file and enumerate all parts. |
| `read_attachment` | Read one part's text, following `next_offset` until null. |
| `view_attachment` | Return real pixels for a PDF page, image/frame or embedded Office image. |

Use the full message event ID from the Buzz prompt.
Attachment indices start at 0; parts start at 1.
Files are fetched again with current access checks for each read; no cross-session document cache is retained.

| Format | Supported reading | Limitations |
| --- | --- | --- |
| PDF | Page text and rendered pages, including visual reading of scans | Password-protected files require an unlocked copy. |
| Word `.docx` | Paragraphs, tables, header/footer paragraphs and embedded images | Not an exact print-layout renderer; legacy `.doc`, tracked-change interpretation, text boxes and footnotes are not guaranteed. |
| Excel `.xlsx` | All sheets including hidden sheets, cell addresses, values, formulas and cached results, embedded images | Does not execute formulas, macros or external links; charts and print layout are not rendered. |
| Excel `.xls` | Stored sheet values | Dates may be Excel serial numbers; embedded drawing extraction is not supported. |
| PNG/JPEG/WebP/GIF/TIFF/BMP | Actual image pixels; multi-frame parts | Rendered to a maximum 2000-pixel edge for model input. |

For layout-dependent Word/Excel interpretation not available from structured content, request a PDF export instead of full manual transcription.
The model must not claim to have read an inaccessible or unsupported part.
The configured model must support images through MCP results; the local Responses configuration uses `gpt-5-mini`.
Model calls send the selected document text/images to the configured provider; consider this when handling confidential files.

## Safety and limits

The harness supplies `BUZZ_CONTEXT_CHANNEL_ID`; a missing scope fails closed.
The service queries a signed kind-9 event in that conversation, verifies its event signature, accepts only the configured relay's content-addressed `/media` URLs, and verifies downloaded SHA-256 bytes.
Other origins, local paths, redirects, query strings and traversal are rejected.
Message queries use NIP-98; file reads use scoped Blossom `t=get` authentication.
Do not expose this service through a public HTTP listener.

File size is capped at 25 MiB, with 20 attachments per message, 400 pages/parts, 50 XLSX sheets, 200,000 cells/200 columns per sheet, and bounded archive expansion.
One parser runs at a time per MCP session, with a 25-second wall timeout, 20-second CPU limit, bounded output and no inherited API keys or Buzz credentials.
Linux additionally applies a parser address-space limit.
This is process isolation and resource bounding, not a full operating-system sandbox; production deployments should also use container memory limits, network isolation and least-privilege filesystem access.
Document content remains untrusted reference data and cannot authorize ERP confirmation.

## Tests

```bash
assistant-runner/.venv/bin/pip install -r assistant-runner/requirements-dev.txt
assistant-runner/.venv/bin/python -m unittest discover -s assistant-runner -v
cargo test -p buzz-acp --lib attachment_scope_overrides
```

`smoke_agent.py --channel <channel UUID> --message <attachment event ID>` exercises the real ACP model with only document tools and the deployed skill catalog.
Source the existing local provider configuration before running it.
It sends document data to the configured model provider but does not publish chat replies or provide any ERP write tool.
Run it only with authorization to use that document with that provider.

For the end-user check, restart the Assistant, send `/reset`, then attach a document with a request to read it and prepare a review-only order draft.
Verify that tools read the actual source, reference lookups precede drafting, only genuinely unresolved facts are requested, and the final response presents an exact `draft_id` without confirming it.
