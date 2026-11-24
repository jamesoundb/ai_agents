---
type: regex
target: "last_message"
pattern: '[Pp]rune[\s\S]{0,250}(delet|remov)\w*[^.\n]{0,80}(object|resource)|(delet|remov)\w*[^.\n]{0,80}(object|resource)[\s\S]{0,250}[Pp]rune|([Ss]ync|Argo CD)[^.\n]{0,40}(delet|remov)\w*[^.\n]{0,40}(object|resource)|[Nn]othing (gets |is |will be )?(deleted|removed)'
match: "contains"
---
The fixture's Application syncs with `automated: {}`; AGENT.md step 3 lists prune/selfHeal as
GitOps-layer fixes, and every run of 2026-11-24 turned `prune: true` on. On a running Application
that makes the sync delete every live object no longer rendered from Git, so the answer must say
so (AGENT.md standard procedure step 3). Two of three runs only wrote "prune and selfHeal turned
on"; a "cascading-delete finalizer" nearby does not count, hence the object/resource anchor.
The third ("automatic sync now removes deleted resources") names it without the word prune.

A run that leaves prune off passes by saying its change deletes nothing ("Nothing gets deleted,
only scaled down", 2026-11-24 after the AGENT.md fix, which edited only values-test.yaml).
