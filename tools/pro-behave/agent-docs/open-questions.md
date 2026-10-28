# Open questions

Each question keeps the ID its source gave it. When one is answered, move the answer into [decisions.md](decisions.md) or its design, and delete it here.

## Two scenario names collide on shared combos

Two scenarios in the corpus share a name with another scenario in the same feature on the same combos, so behave cannot select one of them alone, and those units are refused (DEC-05):

| Feature | Scenario | Ambiguous units |
| --- | --- | --- |
| `features/_version.feature` | Check pro version | 8 |
| `features/api/fix_plan.feature` | Fix command on an unattached machine | 3 |

The fix is to rename the scenarios in `features/`, which is a change to the test suite rather than to this tool. The check itself stays in the domain (DEC-30).

## Architecture review items have not been copied here

The architecture review still lists requirements R2, R7, R8, R10, R11 and R12 as open, and defects D8, D9, D10 and D14. Their text is in the review doc (see [README.md](README.md)), which is readable only with the Claude Docs connector. The next agent with that connector should copy each item here, or into [decisions.md](decisions.md) if it was answered, and delete this section.
