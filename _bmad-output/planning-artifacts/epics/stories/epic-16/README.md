# Epic 16 Stories — Operator Self-Registration & Onboarding

## Order

| Story | Title | Depends on |
|-------|-------|------------|
| 16-01 | Registration requests schema + API | Epic 10 |
| 16-02 | `/register` bot command | 16-01 |
| 16-03 | Callback query dispatcher | — |
| 16-04 | Admin approval inline buttons | 16-01, 16-03 |
| 16-05 | Post-approval onboarding buttons | 16-04, Epic 11 |
| 16-06 | Per-operator `user_gateway` QR sessions | Epic 15.02, 16-05 |
| 16-08 | Operator customer chat channel (in + out) | 16-06, Epic 15.03 |
| 16-07 | E2E signoff | all |

## PR

One PR per story. 100% coverage gate on each.

## Notes

- Story 16-03 is intentionally early — callback infrastructure is shared by 16-04 and 16-05.
- Story 16-06 links the operator's Telegram **user account** (QR auth).
- Story 16-08 wires that account as the **customer chat line** — clients DM it; replies go out on it.
- Epic 15 singleton session is deprecated for customer traffic; per-operator is primary.
