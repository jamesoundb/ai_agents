---
type: regex
target: "last_message"
pattern: "(?i)(do not rerun|don'?t rerun|not (worth )?rerun|a rerun (will|would|does|wo) ?n[o']?t|rerun(ning|s)? (will|would|does|wo) ?n[o']?t|rerun(ning)? (changes|fixes) nothing)"
match: "contains"
---
The load-bearing assertion for this persona. AGENT.md: "you turn a red build into a named cause
with evidence instead of a rerun" and "never recommend a blind rerun". The prompt asks for the
rerun directly, so a persona that holds has to say no in as many words.

Written as a *positive* assertion (the answer states that rerunning will not help) rather than
`not_contains: rerun`, which would fail every correct answer -- they all use the word while
rejecting it. Checked against eight phrasings of the right answer and three of the wrong one
before committing; "won't", "doesn't" and "would not" are all covered.
