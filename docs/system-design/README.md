# System design documents

Word documents describing the marketing agent as of branch `feature/sales-app`.

| File | Contents |
|---|---|
| [01-architecture-overview.docx](01-architecture-overview.docx) | Purpose, system context, building blocks, the two runtimes, design principles, stack |
| [02-backend-design.docx](02-backend-design.docx) | Package layout, inbox service, background jobs (15-minute sync), FAQ trust and auto-send, insights, LLM layer, integrations, daily job |
| [03-frontend-design.docx](03-frontend-design.docx) | Web app files, routing, Chat state and cards, auto-send UX, refresh model, Sales, Marketing and Settings |
| [04-api-contract.docx](04-api-contract.docx) | Every HTTP endpoint: request, response, errors, send semantics |
| [05-data-contract-and-schema.docx](05-data-contract-and-schema.docx) | SQLite tables, relationships, lifecycles, settings, invariants, file and API data contracts |
| [06-end-to-end-workflows.docx](06-end-to-end-workflows.docx) | Boot, inbound reply, auto-send, periodic refresh, daily job, outreach, funnel, brand building, suppression |
| [07-operations-security-and-deployment.docx](07-operations-security-and-deployment.docx) | Configuration, deployment, monitoring runbook, security, limits, known issues |

The Markdown sources live in [source/](source/); edit those, not the .docx files. To rebuild:

```bash
cd docs/system-design
npm install --no-save docx && node build_docx.js source .
```
