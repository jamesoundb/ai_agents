---
type: regex
target: "last_message"
pattern: 'SEC00[125]'
match: "contains"
---
The load-bearing assertion that the tool output was *used* and not merely run.

Rule ids come from the skill's catalogue; a model cannot produce `SEC001` from general
Terraform knowledge. Verified against real output on this fixture (2026-09-29):

    critical SEC001 main.tf:11  ... grants access to allUsers (public)
    critical SEC005 main.tf:31  ... allows ingress from the internet on 22
    high     SEC002 main.tf:17  ... grants primitive role roles/editor

Any one of the three is enough; requiring all three would grade summarization style rather
than whether the report reached the answer.
