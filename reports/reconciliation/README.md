# Reconciliation reports

Dated records of `reconcile.py` runs, kept as an audit trail. Each answers one
question: **does what we recorded doing match what the broker actually holds?**

```
python reconcile.py --days 2 --save
```

Filenames carry the verdict, so the history is skimmable:

| suffix | meaning |
|---|---|
| `CLEAN` | record and broker agree, nothing outstanding |
| `WARN` | differences that are explainable — drift between rebalances, unfilled orders |
| `BREAK` | an order recorded as SENT that the broker has never heard of, or a holding nothing here put on |

A `BREAK` is the one that matters. It means the book is out of step with the
record in a way nobody would otherwise notice — a position missing, or one
present that no strategy asked for.

Account numbers are masked by default because this directory is public and the
identifier adds nothing to the audit value. `--full-account` includes it for an
internal record.

These are **records, not state**. `reconcile.py` reports and never corrects;
silently repairing a break would destroy the evidence of what caused it.
