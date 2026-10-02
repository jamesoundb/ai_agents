package store

// Casing has an exported and an unexported method that differ only in case (Go's
// `Context.Plan` / `Context.plan`): looking up `Casing.Run` must not be ambiguous.
type Casing struct{}

func (c *Casing) Run() int { return c.run() }

func (c *Casing) run() int { return 1 }
