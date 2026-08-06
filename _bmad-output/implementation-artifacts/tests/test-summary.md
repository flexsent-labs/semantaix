# E2E test summary: humanized and RAG-backed vehicle-rental dialogues

## Scope

The customer funnel was tested with natural Telegram-style messages containing
polite filler, uncertainty, clarifications, service typos, and numeric answers.
The acceptance boundary is the final handoff to the operator, not only a
successful answer from the sales answerer.

The RAG-backed suite uses a snapshot of approved chunks from the six imported
Kozlotur documents. It verifies retrieval for квадроциклы, багги and
эндуро/мотоциклы, catalog fallback when the digest is unavailable, OCR price
formats, and the complete HTTP conversation through the operator ticket.

## Scenarios

- Quad bikes: `квадрацыклах` typo, two customers, two vehicles, medium route,
  one driver.
- Buggies: three customers, two vehicles, beginner route, two drivers.
- Motorcycles: two customers, two vehicles, medium route, two drivers.

Each scenario runs through `POST /conversations/inbound`, verifies that no turn
returns `С этим не помогу.`, and verifies that the final turn creates a
`sales_escalation` HITL ticket assigned to `@flexsentlabs` and sends the ticket
details to the operator chat.

## Regression covered

When the bot has asked `Сколько человек поедет?`, a numeric reply such as `2`
is captured before the LLM call. A temporary OpenRouter transport failure can
therefore no longer send the customer to ScopeGuard with `С этим не помогу.`.

## Verification

- `pytest -q -m e2e`: **147 passed**.
- Focused RAG vehicle dialogues, price lookup and repository tests: **43 passed**.
- Full repository run: **4128 passed**.
- `ruff check` on changed production and test files: **passed**.

The remaining test output contains existing FastAPI/calendar deprecation
warnings; no test failures remain in these runs.
