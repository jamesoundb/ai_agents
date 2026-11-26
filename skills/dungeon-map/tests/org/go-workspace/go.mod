module {HOST}/{ORG}/go-workspace

go 1.22

require github.com/upstream/log v1.4.0

replace github.com/upstream/log => {HOST}/{ORG}/go-log-fork v1.4.1
