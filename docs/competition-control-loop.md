# Signalpost competition optimization loop

The objective is a qualified external-first score of at least 80/100 on a frozen 1,000-company submission
and a separate zero-overlap extension corpus. Registry financials are foundation data; verified external
footprint, reviews, jobs, buzz and sentiment are the primary differentiator.

The independent judge score is authoritative. A deterministic local scorer is only the fast feedback
signal between independent reviews. It must never replace the hidden evaluation.

## Loop

1. Measure qualification and the five subscores on frozen artifacts.
2. Identify the largest score loss that can be changed without lowering exact-entity precision.
3. Make one bounded change and add the regression that would catch its most dangerous failure.
4. Rerun the same corpus. Keep the change only when it closes a hard gate or gains at least two proxy points.
5. Rerun the zero-overlap extension corpus. A gain that disappears there is overfitting and is dropped.
6. Request a fresh independent rescore only after a meaningful score/gate movement.

## Controller behavior

- Reinforce: qualification gate closes or same-corpus score rises by at least two without regressions.
- Hold: improvement is under two points but supplies necessary infrastructure for the next measured gate.
- Drop: two iterations produce under one point total, coverage rises by lowering precision, or the extension
  corpus regresses materially.
- Roll back immediately: wrong-company publication, fabricated financial claim, silent entity drop,
  missing material evidence hash, prohibited request, or broken regression gate.

The external-first baseline is 33.968/100 raw and zero awardable because no external observations have
yet passed the new blind audit. The first experiments are Google Places, YouTube, licensed news/search,
and jobs. Each begins on a 100-company development slice, then a zero-overlap validation slice.
